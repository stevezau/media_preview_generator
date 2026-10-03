"""Validation and selection for per-run full-library scan filters."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .servers.base import MediaItem


FILTER_CONFIG_KEYS = frozenset(
    {"added_filter", "added_last_days", "added_from", "added_to", "latest_seasons", "movie_year_from", "movie_year_to"}
)


def _positive_integer(value: Any, name: str, *, maximum: int | None = None) -> int:
    if type(value) is not int or value < 1 or (maximum is not None and value > maximum):
        limit = f" between 1 and {maximum}" if maximum else " greater than zero"
        raise ValueError(f"{name} must be an integer{limit}")
    return value


def _calendar_date(value: Any, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{name} must be a date in YYYY-MM-DD format")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid calendar date") from exc
    return value


def normalize_scan_filter_config(config: Any, *, full_scan: bool = True) -> dict:
    """Validate filters and clear inactive values before persisting a config.

    Args:
        config: Job or schedule configuration; unrelated keys are preserved.
        full_scan: False for Recently Added or Intro & Credits schedules.

    Returns:
        A copy with canonical filter values, or unchanged keys when no filters
        were supplied. Non-full-scan schedules have all filter keys removed.

    Raises:
        ValueError: The config shape or an active filter is invalid.
    """
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise ValueError("config must be an object")
    result = dict(config)
    if not full_scan:
        return {key: value for key, value in result.items() if key not in FILTER_CONFIG_KEYS}
    if not FILTER_CONFIG_KEYS.intersection(result):
        return result

    mode = result.get("added_filter", "all")
    if mode not in ("all", "last_days", "date_range"):
        raise ValueError("added_filter must be all, last_days, or date_range")
    days = start = end = None
    if mode == "last_days":
        days = _positive_integer(result.get("added_last_days"), "added_last_days")
        try:
            datetime.now(UTC) - timedelta(days=days)
        except (OverflowError, ValueError) as exc:
            raise ValueError("added_last_days is too large") from exc
    elif mode == "date_range":
        start = _calendar_date(result.get("added_from"), "added_from")
        end = _calendar_date(result.get("added_to"), "added_to")
        if start > end:
            raise ValueError("added_from must be on or before added_to")

    seasons = result.get("latest_seasons")
    if seasons is not None:
        seasons = _positive_integer(seasons, "latest_seasons")
    year_from, year_to = result.get("movie_year_from"), result.get("movie_year_to")
    if year_from is not None:
        year_from = _positive_integer(year_from, "movie_year_from", maximum=9999)
    if year_to is not None:
        year_to = _positive_integer(year_to, "movie_year_to", maximum=9999)
    if year_from is not None and year_to is not None and year_from > year_to:
        raise ValueError("movie_year_from must be on or before movie_year_to")

    result.update(
        added_filter=mode,
        added_last_days=days,
        added_from=start,
        added_to=end,
        latest_seasons=seasons,
        movie_year_from=year_from,
        movie_year_to=year_to,
    )
    return result


def parse_added_at(value: Any, *, naive_is_utc: bool) -> datetime | None:
    """Normalize a vendor's added date to aware UTC, treating invalid data as unknown.

    PlexAPI returns local naive datetimes; Emby/Jellyfin ISO dates without an
    explicit offset are UTC. Never apply Plex's local interpretation to them.
    """
    try:
        if isinstance(value, str):
            # Emby/Jellyfin may emit seven fractional digits; Python 3.10
            # accepts at most microsecond precision.
            value = re.sub(r"(\.\d{6})\d+", r"\1", value.strip())
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime):
            return None
        if value.tzinfo is None and naive_is_utc:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    except (ValueError, OverflowError, OSError):
        return None


def metadata_integer(value: Any, *, minimum: int = 0) -> int | None:
    """Return integer vendor metadata without coercing missing or malformed values."""
    return value if type(value) is int and value >= minimum else None


def metadata_id(value: Any) -> str | None:
    """Return a usable stable vendor identifier, never a display-title fallback."""
    if isinstance(value, str) and value.strip():
        return value
    if type(value) is int and value > 0:
        return str(value)
    return None


@dataclass(frozen=True)
class ScanFilters:
    """One immutable filter snapshot, including the cutoff time for this job run."""

    added_filter: str = "all"
    added_last_days: int | None = None
    added_from: date | None = None
    added_to: date | None = None
    latest_seasons: int | None = None
    movie_year_from: int | None = None
    movie_year_to: int | None = None
    now: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def from_config(cls, config: object) -> ScanFilters:
        """Validate runtime overrides and freeze relative dates once per full scan."""
        values = normalize_scan_filter_config(vars(config))
        kwargs = {key: values[key] for key in FILTER_CONFIG_KEYS if key in values}
        for key in ("added_from", "added_to"):
            if kwargs.get(key):
                kwargs[key] = date.fromisoformat(kwargs[key])
        return cls(**kwargs)

    @property
    def active(self) -> bool:
        """Whether any filter can exclude an item."""
        return (
            self.added_filter != "all"
            or self.latest_seasons is not None
            or self.movie_year_from is not None
            or self.movie_year_to is not None
        )

    def select(
        self,
        items: Iterable[MediaItem],
        *,
        excluded: Counter[str],
        cancel_check: Callable[[], bool] | None = None,
        library_kind: str | None = None,
    ) -> Iterator[MediaItem]:
        """Preserve enumeration order and versions while applying filters together.

        Latest seasons are determined from the full library before date/year
        filtering, so an old latest season never falls back to a newer-added
        episode from an earlier season. Each call is one server/library scope.
        """
        latest: dict[str, set[int]] = {}
        if self.latest_seasons is not None:
            collected = []
            for item in items:
                if cancel_check and cancel_check():
                    return
                collected.append(item)
                if (
                    self._media_type(item, library_kind) == "episode"
                    and item.series_id
                    and item.season_number
                    and item.season_number > 0
                ):
                    latest.setdefault(item.series_id, set()).add(item.season_number)
            latest = {key: set(sorted(seasons)[-self.latest_seasons :]) for key, seasons in latest.items()}
            items = collected

        cutoff = self.now - timedelta(days=self.added_last_days) if self.added_filter == "last_days" else None
        for item in items:
            if cancel_check and cancel_check():
                return
            reason = self._exclusion_reason(item, latest, cutoff, library_kind)
            if reason:
                excluded[reason] += 1
            else:
                yield item

    @staticmethod
    def _media_type(item: MediaItem, library_kind: str | None) -> str | None:
        if item.media_type:
            return item.media_type
        if library_kind in ("movie", "movies"):
            return "movie"
        if library_kind in ("show", "series", "tvshows", "episode"):
            return "episode"
        return None

    def _exclusion_reason(
        self, item: MediaItem, latest: dict[str, set[int]], cutoff: datetime | None, library_kind: str | None
    ) -> str | None:
        if self.added_filter != "all":
            if item.added_at is None:
                return "missing added date"
            if cutoff is not None and not cutoff <= item.added_at <= self.now:
                return "outside added-date window"
            if self.added_filter == "date_range":
                # astimezone() uses the offset on the item's date, including DST,
                # rather than reusing today's fixed local UTC offset.
                try:
                    local_date = item.added_at.astimezone().date()
                except (ValueError, OverflowError, OSError):
                    return "missing added date"
                if not self.added_from <= local_date <= self.added_to:
                    return "outside added-date window"
        years_active = self.movie_year_from is not None or self.movie_year_to is not None
        media_type = self._media_type(item, library_kind)
        if not media_type and (self.latest_seasons is not None or years_active):
            return "missing media type"
        if media_type == "episode" and self.latest_seasons is not None:
            if item.season_number == 0:
                return "specials excluded"
            if not item.series_id or item.season_number is None:
                return "missing show or season"
            if item.season_number not in latest.get(item.series_id, set()):
                return "outside latest seasons"
        if media_type == "movie" and years_active:
            if item.year is None:
                return "missing movie year"
            if self.movie_year_from is not None and item.year < self.movie_year_from:
                return "outside movie years"
            if self.movie_year_to is not None and item.year > self.movie_year_to:
                return "outside movie years"
        return None
