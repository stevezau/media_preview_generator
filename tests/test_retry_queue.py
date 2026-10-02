"""The global retry policy shared by webhook preview jobs and Intro & Credits retries."""

from unittest.mock import MagicMock

import pytest

from media_preview_generator.processing.retry_queue import (
    BACKOFF_SCHEDULE,
    DEFAULT_RETRY_COUNT,
    retry_policy,
    scaled_backoff_delay,
)


def _settings(values: dict) -> MagicMock:
    sm = MagicMock()
    sm.get.side_effect = lambda key, default=None: values.get(key, default)
    return sm


class TestRetryPolicy:
    @pytest.mark.parametrize(
        ("values", "expected"),
        [
            ({}, (5, 30)),
            ({"webhook_retry_count": 5, "webhook_retry_delay": 120}, (5, 120)),
            ({"webhook_retry_count": "7", "webhook_retry_delay": "45"}, (7, 45)),
            ({"webhook_retry_count": 11, "webhook_retry_delay": 301}, (10, 300)),
            ({"webhook_retry_count": -1, "webhook_retry_delay": 9}, (0, 10)),
            ({"webhook_retry_count": 0, "webhook_retry_delay": 10}, (0, 10)),
            ({"webhook_retry_count": "lots", "webhook_retry_delay": None}, (5, 30)),
            ({"webhook_retry_count": [1], "webhook_retry_delay": "soon"}, (5, 30)),
            ({"webhook_retry_count": 3, "webhook_retry_delay": 30}, (3, 30)),
        ],
        ids=[
            "defaults",
            "in-range",
            "numeric-strings",
            "above-max",
            "below-min",
            "at-min",
            "garbage",
            "wrong-types",
            "stored-3-kept",
        ],
    )
    def test_count_and_delay_are_clamped_and_bad_values_fall_back_to_defaults(self, values, expected):
        assert retry_policy(_settings(values)) == expected


class TestRetryWindow:
    """How long a chain keeps retrying. The default count walks the whole schedule; the old default of 3 stopped
    after 8 minutes, well short of the "~83 minutes" the schedule was written for."""

    def test_default_count_walks_the_whole_schedule(self):
        assert DEFAULT_RETRY_COUNT == 5 == len(BACKOFF_SCHEDULE)

    @pytest.mark.parametrize(
        ("count", "delay_setting", "waits", "total_minutes"),
        [
            (3, 30, [60, 120, 300], 8),
            (5, 30, [60, 120, 300, 900, 3600], 83),
            (3, 60, [120, 240, 600], 16),
            (5, 60, [120, 240, 600, 1800, 7200], 166),
        ],
        ids=["3-retries-default-delay", "5-retries-default-delay", "3-retries-delay-60", "5-retries-delay-60"],
    )
    def test_waits_and_total_window(self, count, delay_setting, waits, total_minutes):
        delays = [scaled_backoff_delay(attempt, delay_setting) for attempt in range(1, count + 1)]

        assert delays == waits
        assert sum(delays) // 60 == total_minutes
