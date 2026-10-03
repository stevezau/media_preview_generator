"""Filter validation, calendar boundaries, season grouping, and streaming."""

import os
import time
from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from media_preview_generator.scan_filters import (
    FILTER_CONFIG_KEYS,
    ScanFilters,
    normalize_scan_filter_config,
    parse_added_at,
)
from media_preview_generator.servers.base import MediaItem

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)


def item(name: str, **kwargs: object) -> MediaItem:
    return MediaItem(id=name, library_id="lib", title="Same display title", remote_path=f"/{name}.mkv", **kwargs)


def select(filters: ScanFilters, items: list[MediaItem], **kwargs: object) -> tuple[list[str], Counter]:
    counts = Counter()
    chosen = list(filters.select(items, excluded=counts, **kwargs))
    return [i.id for i in chosen], counts


@pytest.mark.parametrize("field", ["added_last_days", "latest_seasons", "movie_year_from", "movie_year_to"])
@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "3", [], {}])
def test_rejects_invalid_integer_filters(field: str, value: object) -> None:
    config = {field: value}
    if field == "added_last_days":
        config["added_filter"] = "last_days"
    with pytest.raises(ValueError, match=field):
        normalize_scan_filter_config(config)


@pytest.mark.parametrize(
    "config",
    [
        {"added_filter": "invalid"},
        {"added_filter": True},
        {"added_filter": []},
        {"added_filter": "last_days"},
        {"added_filter": "last_days", "added_last_days": 10**100},
        {"added_filter": "date_range", "added_from": "2026-01-01"},
        {"added_filter": "date_range", "added_from": "2026-02-30", "added_to": "2026-03-01"},
        {"added_filter": "date_range", "added_from": "2026-10-03", "added_to": "2026-10-02"},
        {"added_filter": "date_range", "added_from": "2026-1-1", "added_to": "2026-10-02"},
        {"added_filter": "date_range", "added_from": "2026-01-01T00:00:00Z", "added_to": "2026-10-02"},
        {"movie_year_from": 10000},
        {"movie_year_from": 2026, "movie_year_to": 2025},
    ],
)
def test_rejects_invalid_windows(config: dict) -> None:
    with pytest.raises(ValueError):
        normalize_scan_filter_config(config)


def test_clears_inactive_date_values_and_recent_schedule_filters() -> None:
    config = {
        "added_filter": "all",
        "added_last_days": -1,
        "added_from": "bad",
        "added_to": False,
        "sort_by": "oldest",
        "latest_seasons": 2,
    }
    normalized = normalize_scan_filter_config(config)
    assert normalized["added_last_days"] is normalized["added_from"] is normalized["added_to"] is None
    assert normalized["latest_seasons"] == 2
    assert normalized["sort_by"] == "oldest"
    assert normalize_scan_filter_config(config, full_scan=False) == {"sort_by": "oldest"}
    assert FILTER_CONFIG_KEYS.isdisjoint(normalize_scan_filter_config(config, full_scan=False))
    assert normalize_scan_filter_config({"sort_by": "newest"}) == {"sort_by": "newest"}


def test_runtime_snapshot_has_validated_dates_and_disabled_defaults() -> None:
    assert not ScanFilters.from_config(SimpleNamespace()).active
    filters = ScanFilters.from_config(
        SimpleNamespace(
            added_filter="date_range",
            added_from="0001-01-01",
            added_to="9999-12-31",
            movie_year_to=2020,
        )
    )
    assert filters.added_from == date.min
    assert filters.added_to == date.max
    assert filters.movie_year_from is None
    assert filters.movie_year_to == 2020
    assert filters.now.tzinfo is UTC


def test_relative_window_is_inclusive_and_excludes_future_or_unknown_added_dates() -> None:
    filters = ScanFilters(added_filter="last_days", added_last_days=2, now=NOW)
    cutoff = NOW - timedelta(days=2)
    ids, counts = select(
        filters,
        [
            item("before", added_at=cutoff - timedelta(microseconds=1)),
            item("cutoff", added_at=cutoff),
            item("now", added_at=NOW),
            item("future", added_at=NOW + timedelta(microseconds=1)),
            item("unknown"),
        ],
    )
    assert ids == ["cutoff", "now"]
    assert counts == {"outside added-date window": 2, "missing added date": 1}


@pytest.fixture
def new_york_timezone(monkeypatch: pytest.MonkeyPatch):
    old_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    if old_tz is None:
        monkeypatch.delenv("TZ", raising=False)
    else:
        monkeypatch.setenv("TZ", old_tz)
    time.tzset()


def test_inclusive_calendar_range_uses_historical_dst_offset(new_york_timezone: None) -> None:
    filters = ScanFilters(added_filter="date_range", added_from=date(2024, 3, 10), added_to=date(2024, 3, 10))
    ids, counts = select(
        filters,
        [
            item("before", added_at=datetime(2024, 3, 10, 4, 59, 59, tzinfo=UTC)),
            item("first", added_at=datetime(2024, 3, 10, 5, tzinfo=UTC)),
            item("last", added_at=datetime(2024, 3, 11, 3, 59, 59, tzinfo=UTC)),
            item("after", added_at=datetime(2024, 3, 11, 4, tzinfo=UTC)),
        ],
    )
    assert ids == ["first", "last"]
    assert counts == {"outside added-date window": 2}


def test_vendor_naive_date_interpretation_and_invalid_dates(new_york_timezone: None) -> None:
    naive = datetime(2024, 3, 10, 1, 30)
    assert parse_added_at(naive, naive_is_utc=False) == datetime(2024, 3, 10, 6, 30, tzinfo=UTC)
    assert parse_added_at(naive.isoformat(), naive_is_utc=True) == datetime(2024, 3, 10, 1, 30, tzinfo=UTC)
    assert parse_added_at("2024-03-10T01:30:00+09:00", naive_is_utc=True) == datetime(2024, 3, 9, 16, 30, tzinfo=UTC)
    assert parse_added_at("2024-03-10T01:30:00.1234567Z", naive_is_utc=True) == datetime(
        2024, 3, 10, 1, 30, 0, 123456, tzinfo=UTC
    )
    for invalid in (None, "bad", False, 123):
        assert parse_added_at(invalid, naive_is_utc=True) is None


def test_latest_distinct_seasons_per_show_preserves_versions_and_excludes_specials() -> None:
    items = [
        item(f"{show}-{season}", media_type="episode", series_id=show, season_number=season)
        for show, seasons in (("a", [1, 3, 7, 0]), ("b", [1, 2]))
        for season in seasons
    ]
    items.append(replace(items[2], remote_path="/a-7-4k.mkv"))
    items += [
        item("no-show", media_type="episode", season_number=9),
        item("no-season", media_type="episode", series_id="a"),
    ]
    ids, counts = select(ScanFilters(latest_seasons=2), items)
    assert ids == ["a-3", "a-7", "b-1", "b-2", "a-7"]
    assert counts == {"outside latest seasons": 1, "specials excluded": 1, "missing show or season": 2}


def test_latest_season_is_chosen_before_date_filter_with_no_older_fallback() -> None:
    filters = ScanFilters(added_filter="last_days", added_last_days=3, latest_seasons=1, now=NOW)
    ids, counts = select(
        filters,
        [
            item(
                "a-newest-old", media_type="episode", series_id="a", season_number=5, added_at=NOW - timedelta(days=10)
            ),
            item("a-older-recent", media_type="episode", series_id="a", season_number=4, added_at=NOW),
            item("b-newest", media_type="episode", series_id="b", season_number=2, added_at=NOW),
        ],
    )
    assert ids == ["b-newest"]
    assert counts == {"outside added-date window": 1, "outside latest seasons": 1}


@pytest.mark.parametrize(
    ("lower", "upper", "expected"),
    [(2020, 2021, ["2020", "2021"]), (2020, None, ["2020", "2021", "2022"]), (None, 2020, ["2019", "2020"])],
)
def test_movie_years_are_inclusive_and_apply_only_to_movies(
    lower: int | None, upper: int | None, expected: list[str]
) -> None:
    movies = [item(str(year), media_type="movie", year=year) for year in range(2019, 2023)]
    movies += [
        item("unknown", media_type="movie"),
        item("episode", media_type="episode"),
        item("video", media_type="musicvideo"),
    ]
    ids, counts = select(ScanFilters(movie_year_from=lower, movie_year_to=upper), movies)
    assert ids == expected + ["episode", "video"]
    assert counts["missing movie year"] == 1


def test_filters_combine_for_mixed_libraries_and_unknown_type_fails_closed() -> None:
    filters = ScanFilters(latest_seasons=1, movie_year_from=2020)
    ids, counts = select(
        filters,
        [
            item("film", media_type="movie", year=2020),
            item("old-film", media_type="movie", year=2019),
            item("tv", media_type="episode", series_id="show", season_number=3, year=1900),
            item("old-tv", media_type="episode", series_id="show", season_number=1, year=2026),
            item("unknown"),
        ],
    )
    assert ids == ["film", "tv"]
    assert counts == {"outside movie years": 1, "outside latest seasons": 1, "missing media type": 1}
    assert select(ScanFilters(movie_year_from=2020), [item("film", year=2020)], library_kind="movie")[0] == ["film"]


def test_date_only_filter_streams_without_reading_ahead() -> None:
    def source():
        yield item("first", added_at=NOW)
        raise AssertionError("date-only filters must stream")

    filtered = ScanFilters(added_filter="last_days", added_last_days=1, now=NOW).select(source(), excluded=Counter())
    assert next(filtered).id == "first"
    filtered.close()


def test_latest_season_collection_stops_on_cancellation_before_yielding() -> None:
    seen = []

    def source():
        for number in range(100):
            seen.append(number)
            yield item(str(number), media_type="episode", series_id="show", season_number=number)

    chosen = list(
        ScanFilters(latest_seasons=1).select(source(), excluded=Counter(), cancel_check=lambda: len(seen) >= 3)
    )
    assert chosen == []
    assert seen == [0, 1, 2]


def test_no_filters_keeps_unknown_metadata_and_specials() -> None:
    items = [item("unknown"), item("special", media_type="episode", season_number=0)]
    assert select(ScanFilters(), items) == (["unknown", "special"], {})
