"""Plex jobs that an Analyze request starts at once when Plex is told to run them "as a scheduled task".

Asking Plex to show a new preview means sending it an Analyze request. Plex treats that as a manual run and starts
every job set to "As a scheduled task" straight away, so the app holds the request until Plex's maintenance window.
"""

from __future__ import annotations

import html
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from loguru import logger

from .base import NATIVE_MODES, ServerConfig

if TYPE_CHECKING:
    from .plex import PlexServer

# Longer than the held re-reads job's 10-minute tick, which re-reads it fresh, so publishing always finds it cached.
_SETTINGS_TTL_S = 900.0
_BUTLER_START_PREF = "ButlerStartHour"
_BUTLER_END_PREF = "ButlerEndHour"
_SCHEDULED = "scheduled"
_ASAP = "asap"
HOLD_PREVIEWS_CHECK_ID = "plex_scheduled_jobs_hold_previews"
SET_JOBS_ACTION = "set_plex_jobs_for_previews"


@dataclass(frozen=True)
class PlexJob:
    """One Plex job a manual Analyze starts, with the settings that switch it on."""

    key: str
    label: str
    server_pref: str
    library_pref: str | None


PLEX_JOBS: tuple[PlexJob, ...] = (
    PlexJob("intro", "Intro markers", "GenerateIntroMarkerBehavior", "enableIntroMarkerGeneration"),
    PlexJob("credits", "Credits markers", "GenerateCreditsMarkerBehavior", "enableCreditsMarkerGeneration"),
    PlexJob("loudness", "Loudness analysis", "LoudnessAnalysisBehavior", "enableLoudnessAnalysis"),
    PlexJob("voice", "Voice activity detection", "GenerateVADBehavior", "enableVoiceActivityGeneration"),
    PlexJob("chapter_thumbs", "Chapter thumbnails", "GenerateChapterThumbBehavior", None),
)


@dataclass(frozen=True)
class PlexJobSettings:
    """What Plex is set to run, read from its server-wide and per-library settings.

    ``libraries`` maps a library id to the per-library job switches that library lists; ``window_start`` and
    ``window_end`` are Plex's maintenance window hours (0-23), None when unreadable.
    """

    server: dict[str, str]
    libraries: dict[str, dict[str, bool]]
    window_start: int | None
    window_end: int | None


_cache: dict[str, tuple[float, PlexJobSettings]] = {}
_cache_lock = threading.Lock()


def _hour(value: Any) -> int | None:
    try:
        hour = int(value)
    except (TypeError, ValueError):
        return None
    return hour if 0 <= hour <= 23 else None


def _settings_from_prefs(prefs: dict[str, Any], libraries: dict[str, dict[str, bool]]) -> PlexJobSettings:
    return PlexJobSettings(
        server={job.server_pref: str(prefs[job.server_pref]) for job in PLEX_JOBS if job.server_pref in prefs},
        libraries=libraries,
        window_start=_hour(prefs.get(_BUTLER_START_PREF)),
        window_end=_hour(prefs.get(_BUTLER_END_PREF)),
    )


def read_library_prefs(server: PlexServer, sections: list[Any] | None = None) -> dict[str, dict[str, bool]]:
    """Each movie and TV library's per-library job switches; raises when Plex can't be read.

    ``sections`` are the server's library sections when the caller already listed them.
    """
    from ..plex_client import retry_plex_call
    from .plex import _pref_bool

    wanted = {job.library_pref for job in PLEX_JOBS if job.library_pref}
    libraries: dict[str, dict[str, bool]] = {}
    if sections is None:
        sections = retry_plex_call(server._connect().library.sections, max_retries=0)
    for section in sections:
        if str(getattr(section, "type", "") or "") not in ("movie", "show"):
            continue
        switches: dict[str, bool] = {}
        for setting in retry_plex_call(section.settings, max_retries=0):
            value = _pref_bool(getattr(setting, "value", None))
            if getattr(setting, "id", None) in wanted and value is not None:
                switches[setting.id] = value
        libraries[str(section.key)] = switches
    return libraries


def read_settings(server: PlexServer, *, fresh: bool = False) -> PlexJobSettings | None:
    """Plex's job settings, cached for fifteen minutes per server; None when Plex can't be read.

    ``fresh`` skips the cached value and asks Plex. Only a successful read replaces the cache, so a failed fresh
    read returns the older cached value (None when there is none).
    """
    if not fresh:
        cached = cached_settings(server.id)
        if cached is not None:
            return cached
    try:
        prefs = {str(p.get("id") or ""): p.get("value") for p in server._fetch_prefs() if isinstance(p, dict)}
        settings = _settings_from_prefs(prefs, read_library_prefs(server))
    except Exception as exc:
        logger.debug("Plex job settings unavailable for {}: {}", server.name, exc)
        return cached_settings(server.id) if fresh else None
    with _cache_lock:
        _cache[server.id] = (time.monotonic(), settings)
    return settings


def cached_settings(server_id: str) -> PlexJobSettings | None:
    """The settings read within the last fifteen minutes, without contacting Plex; None when there are none."""
    with _cache_lock:
        cached = _cache.get(server_id)
    return cached[1] if cached is not None and time.monotonic() - cached[0] < _SETTINGS_TTL_S else None


def invalidate(server_id: str) -> None:
    """Forget the cached settings so the next read asks Plex again."""
    with _cache_lock:
        _cache.pop(server_id, None)


def held_jobs(settings: PlexJobSettings, library_id: str | None) -> list[PlexJob]:
    """Jobs an Analyze would start now: set to a scheduled task and switched on in this library.

    A switch the library doesn't list, or an unknown library, counts as on (Plex's default).
    """
    switches = settings.libraries.get(str(library_id), {}) if library_id is not None else {}
    return [
        job
        for job in PLEX_JOBS
        if settings.server.get(job.server_pref) == _SCHEDULED
        and (job.library_pref is None or switches.get(job.library_pref, True))
    ]


def window_open(settings: PlexJobSettings, now: datetime) -> bool:
    """Whether Plex's maintenance window is open at ``now``.

    Plex's hours are compared with this app's clock (``now``); Docker installs share TZ with the Plex host in practice. Equal or unknown
    hours count as open so a request is never stranded.
    """
    start, end = settings.window_start, settings.window_end
    if start is None or end is None or start == end:
        return True
    if start < end:
        return start <= now.hour < end
    return now.hour >= start or now.hour < end


def chapter_thumbnails_on(config: ServerConfig | None) -> bool:
    """Chapter thumbnails register only after Plex has analyzed the file, so a held refresh would starve them."""
    return config is not None and config.output.get("chapter_thumbnails") is True


def should_hold(server: PlexServer, library_id: str | None, now: datetime | None = None) -> bool:
    """Whether a re-read of this library's files has to wait for Plex's maintenance window."""
    if chapter_thumbnails_on(getattr(server, "server_config", None)):
        return False
    settings = read_settings(server)
    if settings is None or not held_jobs(settings, library_id):
        return False
    return not window_open(settings, now or datetime.now())


def job_names(jobs: list[PlexJob]) -> str:
    """The jobs as running text, e.g. ``intro markers, credits markers and loudness analysis``."""
    names = [job.label.lower() for job in jobs]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def window_text(settings: PlexJobSettings) -> str:
    """Plex's maintenance window as ``2:00–5:00``; ``unknown hours`` when Plex didn't say."""
    if settings.window_start is None or settings.window_end is None:
        return "unknown hours"
    return f"{settings.window_start}:00–{settings.window_end}:00"


def readiness_check(
    prefs: dict[str, Any],
    library_prefs: dict[str, dict[str, bool]],
    enabled_library_ids: list[str],
    done_by_app: set[str],
    *,
    can_write: bool,
) -> dict[str, Any] | None:
    """The Setup Health row for jobs that make Plex's re-read wait, or None when there are none.

    Args:
        prefs: Plex's server-wide settings by id (``/:/prefs``).
        library_prefs: Per-library job switches by library id, as in :class:`PlexJobSettings`.
        enabled_library_ids: Libraries this app makes previews for.
        done_by_app: Keys of the jobs this app does itself for this server; those are recommended as Never.
        can_write: Whether this app may change Plex's settings.

    Returns:
        A readiness check, or None when no scheduled job would start in a library this app makes previews for.
    """
    settings = _settings_from_prefs(prefs, library_prefs)
    jobs = [
        job for job in PLEX_JOBS if any(job in held_jobs(settings, library_id) for library_id in enabled_library_ids)
    ]
    if not jobs:
        return None
    recommended = {job.key: "never" if job.key in done_by_app else _ASAP for job in jobs}
    names = job_names(jobs)
    all_asap = set(recommended.values()) == {_ASAP}
    if len(set(recommended.values())) == 1:
        recommended_text = NATIVE_MODES[next(iter(recommended.values()))]
    else:
        recommended_text = " · ".join(f"{job.label}: {NATIVE_MODES[recommended[job.key]]}" for job in jobs)
    check: dict[str, Any] = {
        "id": HOLD_PREVIEWS_CHECK_ID,
        "label": f"New previews only show in Plex after its maintenance window ({window_text(settings)})",
        "docs_anchor": "library-settings",
        "tooltip": "Plex starts jobs set to a scheduled task as soon as the app asks it to show a new preview.",
        "explanation": (
            f"<p>To show a new preview, this app has to ask Plex to re-read the file. Plex starts every job set to "
            f"<em>As a scheduled task</em> ({html.escape(names)}) the moment it is asked, so the app waits for "
            f"Plex's maintenance window instead.</p>"
            "<p>Set these jobs to <em>As a scheduled task and when media is added</em> to see new previews within a "
            "minute; Plex then runs them as soon as new media arrives. Happy to wait for the morning? Dismiss this "
            "row.</p>"
        ),
        "ok": False,
        "severity": "recommended",
        "current": ", ".join(job.label for job in jobs) + ": " + NATIVE_MODES[_SCHEDULED],
        "recommended": recommended_text,
        "reason": (
            f"Plex runs {names} as a scheduled task. Asking Plex to show a new preview would start that work "
            "straight away, so the app waits for the maintenance window."
        ),
        "actions": {},
        "meta": {"jobs": recommended},
    }
    if can_write:
        changes = "".join(f"<li>{html.escape(job.label)}: {NATIVE_MODES[recommended[job.key]]}</li>" for job in jobs)
        check["actions"] = {
            "enable": {
                "action": SET_JOBS_ACTION,
                "args": {},
                "confirm": {
                    "kind": "button",
                    "phrase": "",
                    "body": (
                        f"Changes these server-wide Plex settings (Plex Settings → Library):<ul>{changes}</ul>"
                        "They apply to every library."
                    ),
                },
            }
        }
        check["fix_action"] = "enable"
        check["fix_label"] = "Run them when media is added" if all_asap else "Apply these Plex settings"
        check["bulk"] = False
    return check


def app_jobs(config: ServerConfig) -> set[str]:
    """Keys of the jobs from :data:`PLEX_JOBS` that this app does itself for this server."""
    from ..loudness.settings import load_server_loudness
    from ..markers.ownership import marker_libraries

    jobs: set[str] = set()
    if marker_libraries(config):
        jobs |= {"intro", "credits"}
    if load_server_loudness(config).enabled:
        jobs.add("loudness")
    if config.output.get("chapter_thumbnails") is True:
        jobs.add("chapter_thumbs")
    return jobs


def wanted_prefs(server: PlexServer, config: ServerConfig) -> dict[str, str]:
    """The server-wide values the readiness row recommends, read fresh from Plex, as ``{pref id: value}``.

    Raises:
        Exception: Plex's settings can't be read.
    """
    if chapter_thumbnails_on(config):
        return {}
    prefs = {str(p.get("id") or ""): p.get("value") for p in server._fetch_prefs() if isinstance(p, dict)}
    check = readiness_check(
        prefs,
        read_library_prefs(server),
        [lib.id for lib in config.libraries if lib.enabled],
        app_jobs(config),
        can_write=False,
    )
    if check is None:
        return {}
    by_key = {job.key: job for job in PLEX_JOBS}
    return {by_key[key].server_pref: value for key, value in check["meta"]["jobs"].items()}
