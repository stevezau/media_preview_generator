"""SkipDB read API (ODbL; read-only API use is exempt from the reciprocity term).

``GET /api/segments?imdb_id&season&episode&duration&adjust`` (https://skipdb.tv/docs): ``duration`` is the stream
length in **seconds**; the answer is ``{"segments": {intro, recap, outro, preview}}``, each the best match or null,
with ``match`` = exact (duration within 2 s), shifted (within 15 s, times adjusted for an extra/missing logo),
agnostic (no duration sent) or out-of-range. ``start_ms == end_ms == 0`` is the "confirmed: no such segment" sentinel.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import requests

from ..models import Candidate, MarkerType, MediaIds, Source
from .online import LookupResult, confidence_value, http_session, ms_value, paced_get_json, result_from, valid_imdb
from .ratelimit import SourceLimiter, get_limiter

BASE_URL = "https://api.skipdb.tv/api/segments"
_LABEL = "SkipDB"
# Bump when parsing changes what a stored answer would hold, so files are asked again.
PARSER_VERSION = 1
_SEGMENT_KEYS = (
    ("intro", MarkerType.INTRO),
    ("recap", MarkerType.RECAP),
    ("outro", MarkerType.CREDITS),
    ("preview", MarkerType.PREVIEW),
)
# "agnostic"/"out-of-range" answers describe a different cut of the video; SkipDB R&M "outros" were 7 s tails when
# matched loosely (spec §4), so only duration-confirmed answers count.
_ACCEPTED_MATCHES = frozenset({"exact", "shifted"})


class SkipDbClient:
    """Looks up a movie or episode by imdb id with the file's duration."""

    def __init__(self, *, limiter: SourceLimiter | None = None, session: requests.Session | None = None) -> None:
        """Create a client.

        Args:
            limiter: Pacing limiter (default: the shared ``skipdb`` one).
            session: HTTP session (default: the shared one).
        """
        self._limiter = limiter if limiter is not None else get_limiter("skipdb")
        self._session = session if session is not None else http_session()

    def lookup(
        self,
        ids: MediaIds,
        *,
        duration_ms: int | None,
        priority: int,
        cancel_check: Callable[[], bool] | None = None,
    ) -> LookupResult:
        """Query SkipDB.

        Args:
            ids: External ids (episodes carry the show's ids).
            duration_ms: Duration of this file; required (only duration-matched answers are used).
            priority: Job priority for the limiter.
            cancel_check: Polled while waiting for a request slot.

        Returns:
            The lookup outcome; ``not_applicable`` without an imdb id, season/episode or duration.
        """
        if ids.kind not in ("movie", "episode") or not valid_imdb(ids.imdb):
            return LookupResult("not_applicable", detail=f"{_LABEL} needs a movie or TV episode with an imdb id")
        if not duration_ms or duration_ms <= 0:
            return LookupResult("not_applicable", detail=f"{_LABEL} needs the file duration")
        params: dict[str, object] = {"imdb_id": ids.imdb}
        if ids.is_episode:
            # Season/episode 0 are real SkipDB rows (specials); only the duration match makes them safe to use.
            if ids.season is None or ids.episode is None or ids.season < 0 or ids.episode < 0:
                return LookupResult("not_applicable", detail=f"{_LABEL} needs season and episode numbers")
            params["season"] = ids.season
            params["episode"] = ids.episode
        params["duration"] = round(duration_ms / 1000, 3)
        params["adjust"] = "conservative"
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
        segments = body.get("segments")
        if not isinstance(segments, dict):
            return LookupResult("unavailable", detail=f"{_LABEL} returned an unexpected response")
        return result_from(_candidates(segments))


def _candidates(segments: dict[str, Any]) -> list[Candidate]:
    candidates = []
    for key, mtype in _SEGMENT_KEYS:
        seg = segments.get(key)
        if not isinstance(seg, dict) or seg.get("match") not in _ACCEPTED_MATCHES:
            continue
        start, end = ms_value(seg.get("start_ms")), ms_value(seg.get("end_ms"))
        # end <= start also drops the 0/0 "confirmed none" sentinel.
        if start is None or end is None or end <= start:
            continue
        candidates.append(Candidate(mtype, start, end, Source.SKIPDB, confidence_value(seg.get("confidence"))))
    return candidates
