"""The global retry policy shared by webhook preview jobs and Intro & Credits retries."""

from unittest.mock import MagicMock

import pytest

from media_preview_generator.processing.retry_queue import retry_policy


def _settings(values: dict) -> MagicMock:
    sm = MagicMock()
    sm.get.side_effect = lambda key, default=None: values.get(key, default)
    return sm


class TestRetryPolicy:
    @pytest.mark.parametrize(
        ("values", "expected"),
        [
            ({}, (3, 30)),
            ({"webhook_retry_count": 5, "webhook_retry_delay": 120}, (5, 120)),
            ({"webhook_retry_count": "7", "webhook_retry_delay": "45"}, (7, 45)),
            ({"webhook_retry_count": 11, "webhook_retry_delay": 301}, (10, 300)),
            ({"webhook_retry_count": -1, "webhook_retry_delay": 9}, (0, 10)),
            ({"webhook_retry_count": 0, "webhook_retry_delay": 10}, (0, 10)),
            ({"webhook_retry_count": "lots", "webhook_retry_delay": None}, (3, 30)),
            ({"webhook_retry_count": [1], "webhook_retry_delay": "soon"}, (3, 30)),
        ],
        ids=["defaults", "in-range", "numeric-strings", "above-max", "below-min", "at-min", "garbage", "wrong-types"],
    )
    def test_count_and_delay_are_clamped_and_bad_values_fall_back_to_defaults(self, values, expected):
        assert retry_policy(_settings(values)) == expected
