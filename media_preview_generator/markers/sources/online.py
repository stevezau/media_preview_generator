"""Shared plumbing for online lookups: the result type, the HTTP session and the paced GET every client uses."""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import requests
from loguru import logger

from ..models import Candidate
from .ratelimit import Acquire, SourceLimiter

REQUEST_TIMEOUT_S = 15
# The OpenAPI patterns of IntroDB.app and TheIntroDB require 7+ digits; a shorter id would only buy a 400.
_IMDB_RE = re.compile(r"tt\d{7,}")
_INVALID_JSON = object()
_session: requests.Session | None = None
_session_lock = threading.Lock()


@dataclass(frozen=True)
class LookupResult:
    """Outcome of one online lookup.

    ``ok``/``no_data`` are stored as evidence; ``unavailable``/``not_applicable`` are not (tried again next run).
    ``detail`` is shown to users and logged, so it never carries a response body, URL or API key.
    """

    status: str
    candidates: tuple[Candidate, ...] = ()
    detail: str = ""


def http_session() -> requests.Session:
    """Shared session with an identifying User-Agent (no credentials; keys go per request).

    Returns:
        The process-wide session.
    """
    global _session
    with _session_lock:
        if _session is None:
            from ... import __version__

            _session = requests.Session()
            _session.headers["User-Agent"] = (
                f"MediaPreviewGenerator/{__version__} (+https://github.com/stevezau/media_preview_generator)"
            )
        return _session


_BUDGET_EXHAUSTED_SUFFIX = f" {Acquire.BUDGET_EXHAUSTED.value}"


def is_budget_exhausted(detail: str) -> bool:
    """Whether an ``unavailable`` result's ``detail`` is a limiter refusal for a used-up daily budget.

    ``paced_get_json`` writes this detail as ``f"{label} {acquired.value}"``, so a real budget refusal always ends
    with ``" budget_exhausted"`` — distinct from a rate-window block, a cancellation, or an HTTP/network failure.
    """
    return detail.endswith(_BUDGET_EXHAUSTED_SUFFIX)


def valid_imdb(value: str | None) -> bool:
    """Whether ``value`` is a usable IMDb title id (``tt`` + 7 or more digits)."""
    return bool(value) and _IMDB_RE.fullmatch(value) is not None


def ms_value(value: object) -> int | None:
    """A JSON millisecond offset as a non-negative int, or None for anything else (null, strings, booleans, NaN)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return int(round(value))


def confidence_value(value: object) -> float:
    """A source-reported confidence clamped to 0-1; missing or non-numeric counts as the default 1.0."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return 1.0
    return min(1.0, max(0.0, float(value)))


def paced_get_json(
    label: str,
    url: str,
    *,
    limiter: SourceLimiter,
    session: requests.Session,
    params: Mapping[str, object],
    priority: int,
    cancel_check: Callable[[], bool] | None,
    headers: Mapping[str, str] | None = None,
    auth_refused: str = "",
) -> dict[str, Any] | LookupResult:
    """GET one JSON object through the source's limiter.

    Args:
        label: Source name for details and logs.
        url: Endpoint.
        limiter: The source's limiter; every response (and network error) is recorded on it.
        session: HTTP session.
        params: Query parameters (never secrets).
        priority: Job priority for the limiter.
        cancel_check: Polled while waiting for a slot.
        headers: Extra request headers (may hold the Authorization header; never logged).
        auth_refused: Detail for 401/403 (the status is appended); empty treats them like any other error status.

    Returns:
        The decoded JSON object on HTTP 200, otherwise the LookupResult to return (a JSON-object 404 → ``no_data``).
    """
    acquired = limiter.acquire(priority=priority, cancel_check=cancel_check)
    if acquired is not Acquire.ALLOWED:
        return LookupResult("unavailable", detail=f"{label} {acquired.value}")
    request_headers = {"Accept": "application/json", **(headers or {})}
    try:
        resp = session.get(url, params=params, headers=request_headers, timeout=REQUEST_TIMEOUT_S)
    except ValueError as exc:
        # Raised before anything reaches the server: a header http.client can't encode (UnicodeEncodeError), or
        # requests' InvalidHeader/InvalidURL (both also ValueError). Not a network failure, so the slot is given back
        # and the circuit is left alone. Only the type is kept: the message or traceback can quote the header.
        limiter.refund()
        logger.debug("{} lookup {} could not be sent: {}", label, dict(params), type(exc).__name__)
        return LookupResult("unavailable", detail=f"{label} request could not be sent: {type(exc).__name__}")
    except requests.RequestException as exc:
        limiter.record(None, None)
        logger.debug("{} lookup {} failed: {}", label, dict(params), type(exc).__name__)
        return LookupResult("unavailable", detail=f"{label} network error: {type(exc).__name__}")
    limiter.record(resp.status_code, resp.headers)
    logger.debug("{} lookup {} → HTTP {}", label, dict(params), resp.status_code)
    if resp.status_code in (401, 403) and auth_refused:
        return LookupResult("unavailable", detail=f"{auth_refused} (HTTP {resp.status_code})")
    if resp.status_code not in (200, 404):
        return LookupResult("unavailable", detail=f"{label} HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        body = _INVALID_JSON
    if resp.status_code == 404:
        # The APIs answer "not found" with a JSON object; an HTML page is a proxy or a moved endpoint, not an answer.
        if isinstance(body, dict):
            return LookupResult("no_data")
        return LookupResult("unavailable", detail=f"{label} HTTP 404")
    if body is _INVALID_JSON:
        return LookupResult("unavailable", detail=f"{label} returned invalid JSON")
    if not isinstance(body, dict):
        return LookupResult("unavailable", detail=f"{label} returned an unexpected response")
    return body


def result_from(candidates: list[Candidate]) -> LookupResult:
    """``ok`` with the candidates, or ``no_data`` when the answer held nothing usable."""
    return LookupResult("ok", tuple(candidates)) if candidates else LookupResult("no_data")
