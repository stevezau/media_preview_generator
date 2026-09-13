"""TheIntroDB v3 client (spec §4). Used without written permission — user key optional; must degrade gracefully.

``GET /v3/media?tmdb_id|tvdb_id|imdb_id&season&episode&duration_ms``: each segment type is an array (several entries
are normal); ``start_ms: null`` means the start of the file and ``end_ms: null`` the end of the file. The API picks
the release version closest to ``duration_ms``, so a lookup without the file's duration is never sent.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import requests

from ..models import Candidate, MarkerType, MediaIds, Source
from .online import LookupResult, confidence_value, http_session, ms_value, paced_get_json, result_from, valid_imdb
from .ratelimit import SourceLimiter, get_limiter

BASE_URL = "https://api.theintrodb.org/v3/media"
_LABEL = "TheIntroDB"
_SEGMENT_KEYS = (
    ("intro", MarkerType.INTRO),
    ("recap", MarkerType.RECAP),
    ("credits", MarkerType.CREDITS),
    ("preview", MarkerType.PREVIEW),
)
_API_TYPES = {"movie": "movie", "episode": "tv"}


class TheIntroDbClient:
    """Looks up segments by tmdb/tvdb/imdb id (+ season/episode) and the file's duration."""

    def __init__(
        self,
        api_key: str = "",
        *,
        limiter: SourceLimiter | None = None,
        session: requests.Session | None = None,
    ) -> None:
        """Create a client.

        Args:
            api_key: The user's own TheIntroDB key (optional; sent only as a Bearer header). Surrounding whitespace is
                stripped; a key with any other non-printable-ASCII character is refused at lookup time.
            limiter: Pacing limiter (default: the shared ``theintrodb`` one).
            session: HTTP session (default: the shared one).
        """
        key = (api_key or "").strip()
        # A pasted zero-width space or curly quote would make http.client raise with the header (the key) in the
        # traceback, so such a key is never put in a header at all.
        self._key_invalid = not (key.isascii() and key.isprintable() and not any(ch.isspace() for ch in key))
        self._api_key = "" if self._key_invalid else key
        self._limiter = limiter if limiter is not None else get_limiter("theintrodb")
        self._session = session if session is not None else http_session()

    def lookup(
        self,
        ids: MediaIds,
        *,
        duration_ms: int | None,
        priority: int,
        cancel_check: Callable[[], bool] | None = None,
    ) -> LookupResult:
        """Query TheIntroDB for one movie or episode.

        Args:
            ids: External ids (episodes carry the show's ids).
            duration_ms: Duration of this file; required.
            priority: Job priority for the limiter.
            cancel_check: Polled while waiting for a request slot.

        Returns:
            The lookup outcome; ``not_applicable`` without a usable id, season/episode or duration.
        """
        params = _params(ids, duration_ms)
        if isinstance(params, LookupResult):
            return params
        if self._key_invalid:
            return LookupResult("unavailable", detail=f"{_LABEL} API key contains invalid characters")
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        auth_refused = f"{_LABEL} rejected the API key" if self._api_key else f"{_LABEL} requires an API key"
        body = paced_get_json(
            _LABEL,
            BASE_URL,
            limiter=self._limiter,
            session=self._session,
            params=params,
            priority=priority,
            cancel_check=cancel_check,
            headers=headers,
            auth_refused=auth_refused,
        )
        if isinstance(body, LookupResult):
            return body
        if not _has_echo_fields(body, ids):
            return LookupResult("unavailable", detail=f"{_LABEL} returned an unexpected response")
        if not _answer_matches(body, ids, params):
            return LookupResult("unavailable", detail=f"{_LABEL} answered for a different item")
        return result_from(_candidates(body))


def _params(ids: MediaIds, duration_ms: int | None) -> dict[str, object] | LookupResult:
    if ids.kind not in _API_TYPES:
        return LookupResult("not_applicable", detail=f"{_LABEL} needs a movie or a TV episode")
    if not duration_ms or duration_ms <= 0:
        return LookupResult("not_applicable", detail=f"{_LABEL} needs the file duration")
    params: dict[str, object] = {}
    if ids.tmdb:
        params["tmdb_id"] = ids.tmdb
    elif ids.is_episode and ids.tvdb:  # tvdb ids are a separate id space for movies (spec §5.2)
        params["tvdb_id"] = ids.tvdb
    elif valid_imdb(ids.imdb):
        params["imdb_id"] = ids.imdb
    else:
        return LookupResult("not_applicable", detail=f"{_LABEL} needs a tmdb, tvdb or imdb id")
    if ids.is_episode:
        if ids.season is None or ids.episode is None or ids.season < 1 or ids.episode < 1:
            return LookupResult("not_applicable", detail=f"{_LABEL} needs season and episode numbers from 1")
        params["season"] = ids.season
        params["episode"] = ids.episode
    params["duration_ms"] = int(duration_ms)
    return params


def _has_echo_fields(body: dict[str, Any], ids: MediaIds) -> bool:
    """Every real answer names its ``type`` (and ``season``/``episode`` for TV); ``{}`` or an error object is no answer.

    Treating such a body as ``no_data`` would hide the item for 14 days.
    """
    if body.get("type") is None:
        return False
    return not ids.is_episode or (body.get("season") is not None and body.get("episode") is not None)


def _answer_matches(body: dict[str, Any], ids: MediaIds, params: dict[str, object]) -> bool:
    """The answer's echoed type/ids agree with the request (``tmdb_id`` only when we sent one).

    TMDB movie and TV ids are separate number spaces (the same number can name a movie and a show), so a type or id
    mismatch means the times belong to another title.
    """
    if body["type"] != _API_TYPES[ids.kind]:
        return False
    if "tmdb_id" in params and body.get("tmdb_id") is not None and str(body["tmdb_id"]) != str(params["tmdb_id"]):
        return False
    if ids.is_episode:
        for key in ("season", "episode"):
            if body[key] != params[key]:
                return False
    return True


def _candidates(body: dict[str, Any]) -> list[Candidate]:
    candidates = []
    for key, mtype in _SEGMENT_KEYS:
        segments = body.get(key)
        if not isinstance(segments, list):
            continue
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            raw_start, raw_end = seg.get("start_ms"), seg.get("end_ms")
            if raw_start is None and raw_end is None:
                continue  # "the whole file" is never a skip marker
            start = 0 if raw_start is None else ms_value(raw_start)
            end = None if raw_end is None else ms_value(raw_end)
            if start is None or (raw_end is not None and end is None):
                continue
            if end is not None and end <= start:
                continue
            candidates.append(Candidate(mtype, start, end, Source.THEINTRODB, confidence_value(seg.get("confidence"))))
    return candidates
