"""IntroDB.app client (TV only, series imdb id, anonymous).

``GET /segments?imdb_id&season&episode`` answers one object (or null) per type, with ``start_ms``/``end_ms`` always
present (OpenAPI at https://api.introdb.app/openapi.json). The API takes no duration, so its answers can't be matched
to this file's cut; the decision rules never publish them alone at the default level.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import requests

from ..models import Candidate, MarkerType, MediaIds, Source
from .online import LookupResult, confidence_value, http_session, ms_value, paced_get_json, result_from, valid_imdb
from .ratelimit import SourceLimiter, get_limiter

BASE_URL = "https://api.introdb.app/segments"
_LABEL = "IntroDB"
_SEGMENT_KEYS = (("intro", MarkerType.INTRO), ("recap", MarkerType.RECAP), ("outro", MarkerType.CREDITS))


class IntroDbClient:
    """Looks up one episode by series imdb id + season/episode."""

    def __init__(self, *, limiter: SourceLimiter | None = None, session: requests.Session | None = None) -> None:
        """Create a client.

        Args:
            limiter: Pacing limiter (default: the shared ``introdb`` one).
            session: HTTP session (default: the shared one).
        """
        self._limiter = limiter if limiter is not None else get_limiter("introdb")
        self._session = session if session is not None else http_session()

    def lookup(
        self,
        ids: MediaIds,
        *,
        duration_ms: int | None,
        priority: int,
        cancel_check: Callable[[], bool] | None = None,
    ) -> LookupResult:
        """Query IntroDB.app (duration is accepted for a uniform interface; the API has no such parameter).

        Args:
            ids: External ids (episodes carry the show's ids).
            duration_ms: Unused.
            priority: Job priority for the limiter.
            cancel_check: Polled while waiting for a request slot.

        Returns:
            The lookup outcome; ``not_applicable`` unless this is an episode with an imdb id and season/episode ≥ 1.
        """
        if not ids.is_episode or not valid_imdb(ids.imdb):
            return LookupResult("not_applicable", detail=f"{_LABEL} needs a TV episode with an imdb id")
        if ids.season is None or ids.episode is None or ids.season < 1 or ids.episode < 1:
            return LookupResult("not_applicable", detail=f"{_LABEL} needs season and episode numbers from 1")
        params: dict[str, object] = {"imdb_id": ids.imdb, "season": ids.season, "episode": ids.episode}
        body = paced_get_json(
            _LABEL,
            BASE_URL,
            limiter=self._limiter,
            session=self._session,
            params=params,
            priority=priority,
            cancel_check=cancel_check,
        )
        if isinstance(body, LookupResult):
            return body
        if not _has_documented_fields(body):
            return LookupResult("unavailable", detail=f"{_LABEL} returned an unexpected response")
        if not _answer_matches(body, params):
            return LookupResult("unavailable", detail=f"{_LABEL} answered for a different episode")
        return result_from(_candidates(body))


def _has_documented_fields(body: dict[str, Any]) -> bool:
    """Every real answer echoes the episode and carries the segment keys (null when missing); ``{}`` is no answer.

    Treating such a body as ``no_data`` would hide the item for 14 days.
    """
    if any(body.get(key) is None for key in ("imdb_id", "season", "episode")):
        return False
    return any(key in body for key, _ in _SEGMENT_KEYS)


def _answer_matches(body: dict[str, Any], params: dict[str, object]) -> bool:
    if str(body["imdb_id"]).lower() != str(params["imdb_id"]).lower():
        return False
    return all(body[key] == params[key] for key in ("season", "episode"))


def _candidates(body: dict[str, Any]) -> list[Candidate]:
    candidates = []
    for key, mtype in _SEGMENT_KEYS:
        seg = body.get(key)
        if not isinstance(seg, dict):
            continue
        start, end = ms_value(seg.get("start_ms")), ms_value(seg.get("end_ms"))
        # Unlike TheIntroDB, a missing end is not documented as "end of file", so it is not guessed.
        if start is None or end is None or end <= start:
            continue
        candidates.append(Candidate(mtype, start, end, Source.INTRODB, confidence_value(seg.get("confidence"))))
    return candidates
