import threading
import time

import pytest

from media_preview_generator.markers.sources import ratelimit
from media_preview_generator.markers.sources.ratelimit import Acquire, SourceLimiter, get_limiter, reset_limiters


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(round(s, 3))
        self.t += s


def _limiter(clock, **kw):
    return SourceLimiter(
        "theintrodb", min_interval_s=0.34, clock=clock.now, sleep=clock.sleep, utc_day=lambda: "2026-09-13", **kw
    )


def test_spaces_requests_by_min_interval():
    c = FakeClock()
    lim = _limiter(c)
    assert lim.acquire(priority=2) is Acquire.ALLOWED
    assert lim.acquire(priority=2) is Acquire.ALLOWED
    assert sum(c.slept) == pytest.approx(0.34, abs=0.01)


def test_ratelimit_headers_remaining_zero_waits_for_reset():
    c = FakeClock()
    lim = _limiter(c)
    lim.acquire(priority=2)
    lim.record(200, {"x-ratelimit-limit": "30", "x-ratelimit-remaining": "0", "x-ratelimit-reset": "10"})
    assert lim.acquire(priority=2) is Acquire.ALLOWED
    assert sum(c.slept) == pytest.approx(10, abs=0.5)


def test_429_blocks_using_retry_after_and_returns_blocked_when_wait_too_long():
    c = FakeClock()
    lim = _limiter(c)
    lim.acquire(priority=2)
    lim.record(429, {"Retry-After": "300"})
    assert lim.acquire(priority=2, max_wait_s=60) is Acquire.BLOCKED
    c.t += 301
    assert lim.acquire(priority=2) is Acquire.ALLOWED


def test_usage_headers_reserve_budget_for_webhook_priorities():
    c = FakeClock()
    lim = _limiter(c)
    lim.acquire(priority=3)
    lim.record(200, {"x-usagelimit-limit": "500", "x-usagelimit-remaining": "100"})
    assert lim.acquire(priority=3) is Acquire.BUDGET_EXHAUSTED  # 100 <= 20% of 500
    assert lim.acquire(priority=1) is Acquire.ALLOWED
    assert lim.acquire(priority=2) is Acquire.ALLOWED


def test_daily_budget_zero_exhausts_every_priority_until_next_day():
    c = FakeClock()
    day = {"v": "2026-09-13"}
    lim = SourceLimiter("theintrodb", min_interval_s=0.0, clock=c.now, sleep=c.sleep, utc_day=lambda: day["v"])
    lim.acquire(priority=1)
    lim.record(200, {"x-usagelimit-limit": "500", "x-usagelimit-remaining": "0"})
    assert lim.acquire(priority=1) is Acquire.BUDGET_EXHAUSTED
    day["v"] = "2026-09-14"
    assert lim.acquire(priority=3) is Acquire.ALLOWED


@pytest.mark.parametrize("failure", [500, 502, None])
def test_circuit_opens_after_consecutive_failures_and_closes_after_timeout(failure):
    c = FakeClock()
    lim = _limiter(c, failure_threshold=3, circuit_open_s=600)
    for _ in range(3):
        assert lim.acquire(priority=2) is Acquire.ALLOWED
        lim.record(failure, None)
    assert lim.acquire(priority=2, max_wait_s=5) is Acquire.BLOCKED
    c.t += 601
    assert lim.acquire(priority=2) is Acquire.ALLOWED


def test_success_resets_failure_count():
    c = FakeClock()
    lim = _limiter(c, failure_threshold=2)
    lim.acquire(priority=2)
    lim.record(500, None)
    lim.acquire(priority=2)
    lim.record(200, {})
    lim.acquire(priority=2)
    lim.record(500, None)
    assert lim.acquire(priority=2, max_wait_s=1) is Acquire.ALLOWED


def test_cancel_while_waiting():
    c = FakeClock()
    lim = _limiter(c)
    lim.acquire(priority=2)
    lim.record(200, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "10"})
    assert lim.acquire(priority=2, cancel_check=lambda: True) is Acquire.CANCELLED


def test_usage_callback_receives_header_values():
    c = FakeClock()
    seen = []
    lim = _limiter(c, on_usage=lambda *a: seen.append(a))
    lim.acquire(priority=2)
    lim.record(200, {"x-usagelimit-limit": "500", "x-usagelimit-remaining": "417"})
    assert seen[-1] == ("theintrodb", "2026-09-13", 1, 500, 417)
    assert lim.usage()["used"] == 1 and lim.usage()["remaining"] == 417


def test_priority_low_mirrors_web_jobs_constant():
    from media_preview_generator.web.jobs import PRIORITY_LOW, PRIORITY_NORMAL

    assert ratelimit.PRIORITY_LOW == PRIORITY_LOW
    assert PRIORITY_NORMAL < ratelimit.PRIORITY_LOW


class TestDailyBudget:
    def test_low_priority_may_spend_down_to_the_reserve_boundary_exactly(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.record(200, {"x-usagelimit-limit": "500", "x-usagelimit-remaining": "101"})
        assert lim.acquire(priority=3) is Acquire.ALLOWED  # 101 > 100 -> allowed, now 100 left
        assert lim.acquire(priority=3) is Acquire.BUDGET_EXHAUSTED
        assert lim.usage()["remaining"] == 100

    def test_reserve_fraction_is_configurable(self):
        c = FakeClock()
        lim = _limiter(c, reserve_fraction=0.5)
        lim.record(200, {"x-usagelimit-limit": "500", "x-usagelimit-remaining": "250"})
        assert lim.acquire(priority=3) is Acquire.BUDGET_EXHAUSTED
        lim.record(200, {"x-usagelimit-limit": "500", "x-usagelimit-remaining": "251"})
        assert lim.acquire(priority=3) is Acquire.ALLOWED

    def test_reserve_uses_the_reported_limit_not_a_hard_coded_one(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.record(200, {"x-usagelimit-limit": "1000", "x-usagelimit-remaining": "150"})
        assert lim.acquire(priority=3) is Acquire.BUDGET_EXHAUSTED  # 150 <= 200
        lim.record(200, {"x-usagelimit-limit": "100", "x-usagelimit-remaining": "150"})
        assert lim.acquire(priority=3) is Acquire.ALLOWED  # 150 > 20

    def test_normal_and_high_priorities_spend_the_reserve_down_to_zero(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.record(200, {"x-usagelimit-limit": "500", "x-usagelimit-remaining": "2"})
        assert lim.acquire(priority=2) is Acquire.ALLOWED
        assert lim.acquire(priority=1) is Acquire.ALLOWED
        assert lim.acquire(priority=1) is Acquire.BUDGET_EXHAUSTED
        assert lim.usage()["remaining"] == 0

    def test_limit_unknown_lets_low_priority_spend_until_zero(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.record(200, {"x-usagelimit-remaining": "1"})
        assert lim.acquire(priority=3) is Acquire.ALLOWED
        assert lim.acquire(priority=3) is Acquire.BUDGET_EXHAUSTED

    def test_negative_remaining_counts_as_exhausted(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.record(200, {"x-usagelimit-limit": "500", "x-usagelimit-remaining": "-3"})
        assert lim.acquire(priority=1) is Acquire.BUDGET_EXHAUSTED
        assert lim.usage()["remaining"] == 0

    def test_zero_or_negative_limit_header_is_ignored(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.record(200, {"x-usagelimit-limit": "500"})
        lim.record(200, {"x-usagelimit-limit": "0", "x-usagelimit-remaining": "100"})
        assert lim.usage()["limit"] == 500
        assert lim.acquire(priority=3) is Acquire.BUDGET_EXHAUSTED

    def test_live_theintrodb_headers_keep_the_budget_until_the_day_rolls(self):
        """Header set captured from TheIntroDB on 2026-09-14: usage reset is sent as "0" mid-day."""
        c = FakeClock()
        lim = _limiter(c)
        lim.record(
            200,
            {
                "x-ratelimit-limit": "30",
                "x-ratelimit-remaining": "29",
                "x-ratelimit-reset": "10",
                "x-usagelimit-limit": "500",
                "x-usagelimit-remaining": "100",
                "x-usagelimit-reset": "0",
                "x-usagelimit-specificmedia-limit": "2000",
                "x-usagelimit-specificmedia-remaining": "1997",
                "x-usagelimit-specificmedia-reset": "0",
            },
        )
        c.t += 7200
        assert lim.acquire(priority=3) is Acquire.BUDGET_EXHAUSTED
        assert lim.usage() == {
            "day": "2026-09-13",
            "used": 0,
            "limit": 500,
            "remaining": 100,
            "blocked_until_s": 0.0,
            "low_priority_exhausted": True,  # 100 remaining is exactly the 20% reserve
            "resets_at": ratelimit.RESET_TIME_LABEL,
        }
        assert lim.acquire(priority=2) is Acquire.ALLOWED
        assert c.slept == []

    def test_day_roll_resets_used_and_remaining_but_keeps_limit(self):
        c = FakeClock()
        day = {"v": "2026-09-13"}
        lim = SourceLimiter("skipdb", min_interval_s=0.0, clock=c.now, sleep=c.sleep, utc_day=lambda: day["v"])
        lim.acquire(priority=2)
        lim.record(200, {"x-usagelimit-limit": "500", "x-usagelimit-remaining": "10"})
        day["v"] = "2026-09-14"
        assert lim.usage() == {
            "day": "2026-09-14",
            "used": 0,
            "limit": 500,
            "remaining": None,
            "blocked_until_s": 0.0,
            "low_priority_exhausted": False,  # remaining unknown again after the roll: nothing to be exhausted yet
            "resets_at": ratelimit.RESET_TIME_LABEL,
        }

    def test_record_after_midnight_counts_toward_the_new_day(self):
        c = FakeClock()
        day = {"v": "2026-09-13"}
        seen = []
        lim = SourceLimiter(
            "skipdb",
            min_interval_s=0.0,
            clock=c.now,
            sleep=c.sleep,
            utc_day=lambda: day["v"],
            on_usage=lambda *a: seen.append(a),
        )
        lim.acquire(priority=2)
        day["v"] = "2026-09-14"
        lim.record(200, {"x-usagelimit-limit": "500", "x-usagelimit-remaining": "499"})
        assert seen[-1] == ("skipdb", "2026-09-14", 0, 500, 499)
        assert lim.acquire(priority=3) is Acquire.ALLOWED
        assert lim.usage()["remaining"] == 498


class TestLowPriorityExhausted:
    """``usage()["low_priority_exhausted"]`` and the standalone helper the Settings API reuses on merged values."""

    def test_usage_flips_on_at_the_reserve_boundary(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.record(200, {"x-usagelimit-limit": "500", "x-usagelimit-remaining": "101"})
        assert lim.usage()["low_priority_exhausted"] is False
        lim.record(200, {"x-usagelimit-limit": "500", "x-usagelimit-remaining": "100"})
        assert lim.usage()["low_priority_exhausted"] is True

    def test_usage_is_exhausted_even_without_a_limit_once_remaining_hits_zero(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.record(200, {"x-usagelimit-remaining": "0"})
        assert lim.usage()["low_priority_exhausted"] is True

    def test_usage_resets_at_is_the_day_boundary_not_a_header(self):
        c = FakeClock()
        lim = _limiter(c)
        # TheIntroDB sends x-usagelimit-reset as "0" while most of the budget remains (see _roll_day); the reported
        # reset must never come from it.
        lim.record(200, {"x-usagelimit-limit": "500", "x-usagelimit-remaining": "100", "x-usagelimit-reset": "0"})
        assert lim.usage()["resets_at"] == ratelimit.RESET_TIME_LABEL == "00:00 UTC"

    @pytest.mark.parametrize(
        ("limit", "remaining", "expected"),
        [
            (None, None, False),  # never learned: nothing to be exhausted yet
            (500, None, False),
            (500, 101, False),
            (500, 100, True),  # exactly the 20% reserve
            (500, 0, True),
            (500, -3, True),
            (None, 0, True),  # remaining alone still means exhausted, limit or not
        ],
        ids=["unknown", "limit-only", "above-reserve", "at-reserve", "zero", "negative", "zero-no-limit"],
    )
    def test_standalone_helper_matches_usage(self, limit, remaining, expected):
        assert ratelimit.low_priority_exhausted(limit=limit, remaining=remaining) is expected

    def test_standalone_helper_honours_a_custom_reserve_fraction(self):
        assert ratelimit.low_priority_exhausted(limit=500, remaining=250, reserve_fraction=0.5) is True
        assert ratelimit.low_priority_exhausted(limit=500, remaining=251, reserve_fraction=0.5) is False


class TestHeaderParsing:
    def test_header_names_are_case_insensitive(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.record(200, {"X-UsageLimit-Limit": "500", "X-UsageLimit-Remaining": "7"})
        assert (lim.usage()["limit"], lim.usage()["remaining"]) == (500, 7)

    @pytest.mark.parametrize("junk", ["inf", "-inf", "nan", "abc", "", "  ", "1e400"])
    def test_junk_values_are_ignored_without_raising(self, junk):
        c = FakeClock()
        lim = _limiter(c)
        lim.record(
            200,
            {
                "x-usagelimit-limit": junk,
                "x-usagelimit-remaining": junk,
                "x-ratelimit-remaining": junk,
                "x-ratelimit-reset": junk,
            },
        )
        lim.record(429, {"retry-after": junk})
        assert lim.usage()["limit"] is None and lim.usage()["remaining"] is None
        assert lim.usage()["blocked_until_s"] == pytest.approx(300.0)

    def test_fractional_values_truncate(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.record(200, {"x-usagelimit-limit": "500.9", "x-usagelimit-remaining": "41.7"})
        assert (lim.usage()["limit"], lim.usage()["remaining"]) == (500, 41)

    def test_ratelimit_remaining_above_zero_does_not_wait(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.record(200, {"x-ratelimit-remaining": "1", "x-ratelimit-reset": "10"})
        assert lim.acquire(priority=2) is Acquire.ALLOWED
        assert c.slept == []

    def test_ratelimit_reset_as_unix_time_is_converted_with_the_wall_clock(self):
        c = FakeClock()
        lim = _limiter(c, wall_clock=lambda: 1_757_800_000.0)
        lim.record(200, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1757800007"})
        assert lim.usage()["blocked_until_s"] == pytest.approx(7.0)

    # "-0000" parses to a naive datetime; it must still be read as UTC, not the host's local time.
    @pytest.mark.parametrize("date", ["Mon, 14 Sep 2026 00:02:00 GMT", "Mon, 14 Sep 2026 00:02:00 -0000"])
    def test_retry_after_http_date_is_converted_with_the_wall_clock(self, date, monkeypatch):
        c = FakeClock()
        # 2026-09-14 00:00:00 UTC
        lim = _limiter(c, wall_clock=lambda: 1_789_344_000.0)
        try:
            with monkeypatch.context() as mp:
                mp.setenv("TZ", "America/New_York")
                time.tzset()
                lim.record(429, {"Retry-After": date})
        finally:
            time.tzset()
        assert lim.usage()["blocked_until_s"] == pytest.approx(120.0)

    def test_retry_after_in_the_past_still_waits_the_minimum(self):
        c = FakeClock()
        lim = _limiter(c, wall_clock=lambda: 1_789_344_000.0)
        lim.record(429, {"Retry-After": "Sun, 13 Sep 2026 23:00:00 GMT"})
        assert lim.usage()["blocked_until_s"] == pytest.approx(1.0)


class Test429:
    def test_retry_after_zero_waits_one_second_not_the_default(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.record(429, {"Retry-After": "0"})
        assert lim.usage()["blocked_until_s"] == pytest.approx(1.0)

    def test_without_retry_after_the_ratelimit_reset_is_used(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.record(429, {"x-ratelimit-reset": "8"})
        assert lim.usage()["blocked_until_s"] == pytest.approx(8.0)

    def test_retry_after_wins_over_ratelimit_reset(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.record(429, {"Retry-After": "45", "x-ratelimit-reset": "8"})
        assert lim.usage()["blocked_until_s"] == pytest.approx(45.0)

    def test_without_any_hint_blocks_for_the_default(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.record(429, None)
        assert lim.usage()["blocked_until_s"] == pytest.approx(300.0)

    def test_block_is_capped_at_one_hour(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.record(429, {"Retry-After": "86400"})
        assert lim.usage()["blocked_until_s"] == pytest.approx(3600.0)

    def test_wait_exactly_at_max_wait_is_allowed(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.record(429, {"Retry-After": "60"})
        assert lim.acquire(priority=2, max_wait_s=60) is Acquire.ALLOWED
        assert sum(c.slept) == pytest.approx(60.0)

    def test_a_shorter_block_never_shortens_a_longer_one(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.record(429, {"Retry-After": "300"})
        lim.record(429, {"Retry-After": "5"})
        assert lim.usage()["blocked_until_s"] == pytest.approx(300.0)

    def test_429_does_not_count_toward_the_circuit(self):
        c = FakeClock()
        lim = _limiter(c, failure_threshold=2)
        lim.record(500, None)
        lim.record(429, {"Retry-After": "1"})
        lim.record(500, None)
        c.t += 2
        assert lim.acquire(priority=2, max_wait_s=0) is Acquire.ALLOWED

    def test_retry_after_on_503_is_honoured(self):
        c = FakeClock()
        lim = _limiter(c, failure_threshold=5)
        lim.record(503, {"Retry-After": "120"})
        assert lim.usage()["blocked_until_s"] == pytest.approx(120.0)


class TestCircuit:
    def test_one_failure_short_of_the_threshold_stays_closed(self):
        c = FakeClock()
        lim = _limiter(c)  # default threshold 5
        for _ in range(4):
            lim.record(None, None)
        assert lim.acquire(priority=2, max_wait_s=0) is Acquire.ALLOWED
        lim.record(504, None)
        assert lim.acquire(priority=2, max_wait_s=60) is Acquire.BLOCKED
        assert lim.usage()["blocked_until_s"] == pytest.approx(600.0)

    def test_circuit_open_duration_is_configurable(self):
        c = FakeClock()
        lim = _limiter(c, failure_threshold=1, circuit_open_s=100)
        lim.record(500, None)
        c.t += 99
        assert lim.acquire(priority=2, max_wait_s=0) is Acquire.BLOCKED
        c.t += 2
        assert lim.acquire(priority=2, max_wait_s=0) is Acquire.ALLOWED

    def test_first_failure_after_the_pause_reopens_immediately(self):
        c = FakeClock()
        lim = _limiter(c, failure_threshold=3, circuit_open_s=600)
        for _ in range(3):
            lim.record(500, None)
        c.t += 601
        lim.record(500, None)
        assert lim.acquire(priority=2, max_wait_s=60) is Acquire.BLOCKED

    def test_success_after_the_pause_needs_a_full_run_of_failures_again(self):
        c = FakeClock()
        lim = _limiter(c, failure_threshold=3, circuit_open_s=600)
        for _ in range(3):
            lim.record(500, None)
        c.t += 601
        lim.record(200, {})
        lim.record(500, None)
        lim.record(500, None)
        assert lim.acquire(priority=2, max_wait_s=0) is Acquire.ALLOWED

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 499])
    def test_client_errors_reset_the_failure_run(self, status):
        c = FakeClock()
        lim = _limiter(c, failure_threshold=2)
        lim.record(500, None)
        lim.record(status, {})
        lim.record(500, None)
        assert lim.acquire(priority=2, max_wait_s=0) is Acquire.ALLOWED

    def test_opening_is_logged_once_without_headers(self, loguru_caplog):
        c = FakeClock()
        lim = _limiter(c, failure_threshold=2)
        for _ in range(4):
            lim.record(500, None)
        opened = [r for r in loguru_caplog.records if "circuit" in r.getMessage().lower()]
        assert len(opened) == 1 and "theintrodb" in opened[0].getMessage()


class TestWaiting:
    def test_waits_in_short_polls_so_cancel_is_prompt(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.record(200, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "10"})
        polls = []

        def cancel():
            polls.append(c.t)
            return len(polls) > 3

        assert lim.acquire(priority=2, cancel_check=cancel) is Acquire.CANCELLED
        assert max(c.slept) <= 0.5
        assert c.t - 1000.0 <= 1.5

    def test_cancel_refunds_the_usage_count(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.record(200, {"x-usagelimit-limit": "500", "x-usagelimit-remaining": "300"})
        assert lim.acquire(priority=2) is Acquire.ALLOWED
        calls = []
        assert lim.acquire(priority=2, cancel_check=lambda: calls.append(1) or len(calls) > 1) is Acquire.CANCELLED
        assert (lim.usage()["used"], lim.usage()["remaining"]) == (1, 299)

    def test_cancel_does_not_refund_over_fresher_server_numbers(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.record(200, {"x-usagelimit-limit": "500", "x-usagelimit-remaining": "300"})
        lim.acquire(priority=2)
        calls = []

        def cancel():
            calls.append(1)
            if len(calls) == 2:
                lim.record(200, {"x-usagelimit-remaining": "250"})
                return True
            return False

        assert lim.acquire(priority=2, cancel_check=cancel) is Acquire.CANCELLED
        assert lim.usage()["remaining"] == 250

    def test_already_cancelled_reserves_nothing(self):
        c = FakeClock()
        lim = _limiter(c)
        assert lim.acquire(priority=2, cancel_check=lambda: True) is Acquire.CANCELLED
        assert lim.usage()["used"] == 0
        assert lim.acquire(priority=2) is Acquire.ALLOWED
        assert c.slept == []

    def test_429_arriving_while_waiting_returns_blocked_and_refunds(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.acquire(priority=2)
        real_sleep = c.sleep

        def sleep(s):
            if not c.slept:
                lim.record(429, {"Retry-After": "300"})
            real_sleep(s)

        lim._sleep = sleep
        assert lim.acquire(priority=2, max_wait_s=60) is Acquire.BLOCKED
        assert lim.usage()["used"] == 1

    def test_rate_window_closing_while_waiting_moves_the_slot(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.acquire(priority=2)
        real_sleep = c.sleep

        def sleep(s):
            if not c.slept:
                lim.record(200, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "5"})
            real_sleep(s)

        lim._sleep = sleep
        assert lim.acquire(priority=2) is Acquire.ALLOWED
        assert c.t == pytest.approx(1005.0, abs=0.01)
        assert lim.acquire(priority=2) is Acquire.ALLOWED
        assert c.t == pytest.approx(1005.34, abs=0.01)

    def test_on_usage_failure_is_logged_and_does_not_break_pacing(self, loguru_caplog):
        c = FakeClock()

        def boom(*_):
            raise RuntimeError("disk full")

        lim = _limiter(c, on_usage=boom)
        lim.acquire(priority=2)
        lim.record(200, {"x-usagelimit-remaining": "5"})
        assert lim.usage()["remaining"] == 5
        assert any("disk full" in r.getMessage() for r in loguru_caplog.records)

    def test_on_usage_is_called_for_failures_too(self):
        c = FakeClock()
        seen = []
        lim = _limiter(c, on_usage=lambda *a: seen.append(a))
        lim.acquire(priority=2)
        lim.record(None, None)
        assert seen == [("theintrodb", "2026-09-13", 1, None, None)]

    def test_stale_usage_snapshot_is_never_published_after_a_newer_one(self):
        c = FakeClock()
        seen = []
        lim = _limiter(c, on_usage=lambda *a: seen.append(a))
        lim._publish_usage(2, ("2026-09-13", 2, 500, 10))
        lim._publish_usage(1, ("2026-09-13", 1, 500, 11))
        assert seen == [("theintrodb", "2026-09-13", 2, 500, 10)]


class TestRefund:
    def test_refund_gives_back_the_last_allowed_reservation(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.record(200, {"x-usagelimit-limit": "500", "x-usagelimit-remaining": "300"})
        assert lim.acquire(priority=2) is Acquire.ALLOWED
        lim.refund()
        assert (lim.usage()["used"], lim.usage()["remaining"]) == (0, 300)

    def test_refund_twice_gives_back_only_once(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.record(200, {"x-usagelimit-remaining": "300"})
        lim.acquire(priority=2)
        lim.acquire(priority=2)
        lim.refund()
        lim.refund()
        assert (lim.usage()["used"], lim.usage()["remaining"]) == (1, 299)

    def test_refund_without_a_reservation_is_a_no_op(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.record(200, {"x-usagelimit-remaining": "300"})
        lim.refund()
        assert (lim.usage()["used"], lim.usage()["remaining"]) == (0, 300)

    def test_refund_after_a_refused_acquire_does_not_touch_the_earlier_sent_request(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.record(200, {"x-usagelimit-limit": "500", "x-usagelimit-remaining": "300"})
        assert lim.acquire(priority=2) is Acquire.ALLOWED
        # The 429 is answered on another thread, so this thread's ticket is only cleared by the refused acquire.
        other = threading.Thread(target=lambda: lim.record(429, {"Retry-After": "300"}))
        other.start()
        other.join(5)
        assert lim.acquire(priority=2) is Acquire.BLOCKED
        lim.refund()
        assert (lim.usage()["used"], lim.usage()["remaining"]) == (1, 299)

    def test_refund_after_record_is_a_no_op(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.acquire(priority=2)
        lim.record(200, {"x-usagelimit-remaining": "300"})
        lim.refund()
        assert (lim.usage()["used"], lim.usage()["remaining"]) == (1, 300)

    def test_refund_keeps_a_fresher_server_count(self):
        c = FakeClock()
        lim = _limiter(c)
        lim.record(200, {"x-usagelimit-remaining": "300"})
        assert lim.acquire(priority=2) is Acquire.ALLOWED
        other = threading.Thread(target=lambda: lim.record(200, {"x-usagelimit-remaining": "250"}))
        other.start()
        other.join(5)
        lim.refund()
        assert (lim.usage()["used"], lim.usage()["remaining"]) == (0, 250)

    def test_refund_only_returns_the_calling_threads_reservation(self):
        c = FakeClock()
        lim = _limiter(c)
        assert lim.acquire(priority=2) is Acquire.ALLOWED
        other = threading.Thread(target=lim.refund)
        other.start()
        other.join(5)
        assert lim.usage()["used"] == 1

    def test_refund_after_midnight_does_not_reduce_the_new_days_count(self):
        c = FakeClock()
        day = {"v": "2026-09-13"}
        lim = SourceLimiter("skipdb", min_interval_s=0.0, clock=c.now, sleep=c.sleep, utc_day=lambda: day["v"])
        assert lim.acquire(priority=2) is Acquire.ALLOWED
        day["v"] = "2026-09-14"
        other = threading.Thread(target=lambda: lim.acquire(priority=2))
        other.start()
        other.join(5)
        lim.refund()
        assert lim.usage() == {
            "day": "2026-09-14",
            "used": 1,
            "limit": None,
            "remaining": None,
            "blocked_until_s": 0.0,
            "low_priority_exhausted": False,
            "resets_at": ratelimit.RESET_TIME_LABEL,
        }


class _SlowSpacingLimiter(SourceLimiter):
    """Sleeps while ``min_interval_s`` is read -- between reading and writing the next free slot.

    Under the GIL the unlocked read-modify-write window is a few bytecodes and races almost never show; the sleep
    releases the GIL inside that window, so a missing lock double-books slots on every run.
    """

    @property
    def min_interval_s(self):
        time.sleep(0.001)
        return self._spacing

    @min_interval_s.setter
    def min_interval_s(self, value):
        self._spacing = value


def _run_threads(count, target):
    threads = [threading.Thread(target=target) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not any(t.is_alive() for t in threads)


class TestConcurrency:
    def test_32_threads_never_share_a_slot(self):
        """Frozen clock: only the thread holding the current slot may go; the rest wait or are refused.

        With min_interval 1 s and max_wait 15 s there are exactly 16 slots (t+0 .. t+15). A race that
        double-books a slot would let a second thread through without waiting, or admit a 17th.
        """
        waited = threading.local()
        lim = _SlowSpacingLimiter(
            "skipdb",
            min_interval_s=1.0,
            clock=lambda: 1000.0,
            sleep=lambda _s: setattr(waited, "yes", True),
            utc_day=lambda: "2026-09-13",
        )
        barrier = threading.Barrier(32)
        results = []
        results_lock = threading.Lock()

        def worker():
            barrier.wait()
            outcome = lim.acquire(priority=2, max_wait_s=15.0, cancel_check=lambda: getattr(waited, "yes", False))
            with results_lock:
                results.append(outcome)

        _run_threads(32, worker)
        assert results.count(Acquire.ALLOWED) == 1
        assert results.count(Acquire.CANCELLED) == 15
        assert results.count(Acquire.BLOCKED) == 16
        assert lim.usage()["used"] == 1

    def test_threads_waiting_when_a_429_lands_replan_to_distinct_slots(self):
        """32 threads hold slots 1001..1032 and are parked in sleep when a 429 (Retry-After 50) arrives.

        Each must move behind the block to its own slot (1050..1081), so the next caller's first free slot is 1082.
        """
        local = threading.local()
        parked = threading.Barrier(33)
        release = threading.Event()

        def sleep(_s):
            if not getattr(local, "parked", False):
                local.parked = True
                parked.wait()
                release.wait()
            else:
                local.done = True

        lim = _SlowSpacingLimiter(
            "skipdb", min_interval_s=1.0, clock=lambda: 1000.0, sleep=sleep, utc_day=lambda: "2026-09-13"
        )
        assert lim.acquire(priority=2) is Acquire.ALLOWED  # takes slot 1000
        results = []
        results_lock = threading.Lock()

        def worker():
            outcome = lim.acquire(priority=2, max_wait_s=100.0, cancel_check=lambda: getattr(local, "done", False))
            with results_lock:
                results.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(32)]
        for t in threads:
            t.start()
        parked.wait()
        lim.record(429, {"Retry-After": "50"})
        release.set()
        for t in threads:
            t.join(10)
        assert results == [Acquire.CANCELLED] * 32
        calls = []
        probe = lim.acquire(priority=2, max_wait_s=81.5, cancel_check=lambda: calls.append(1) or len(calls) > 1)
        assert probe is Acquire.BLOCKED  # slot 1082 > 1000 + 81.5
        assert lim.usage()["used"] == 1

    def test_32_threads_on_a_real_clock_are_spread_over_the_interval(self):
        lim = SourceLimiter("skipdb", min_interval_s=0.02, utc_day=lambda: "2026-09-13")
        barrier = threading.Barrier(32)
        times = []
        lock = threading.Lock()
        t0 = time.monotonic()

        def worker():
            barrier.wait()
            outcome = lim.acquire(priority=2, max_wait_s=5.0)
            with lock:
                times.append((outcome, time.monotonic()))

        _run_threads(32, worker)
        assert [outcome for outcome, _ in times] == [Acquire.ALLOWED] * 32
        # Slots are t0' + k*0.02 with t0' >= t0, and nobody returns before its slot.
        assert max(t for _, t in times) - t0 >= 31 * 0.02
        assert lim.usage()["used"] == 32

    def test_budget_is_never_overspent_under_contention(self):
        lim = _SlowSpacingLimiter("theintrodb", min_interval_s=0.0, utc_day=lambda: "2026-09-13")
        lim.record(200, {"x-usagelimit-limit": "500", "x-usagelimit-remaining": "110"})
        barrier = threading.Barrier(32)
        results = []
        lock = threading.Lock()

        def worker():
            barrier.wait()
            outcome = lim.acquire(priority=3)
            with lock:
                results.append(outcome)

        _run_threads(32, worker)
        assert results.count(Acquire.ALLOWED) == 10  # 110 -> 100 (the 20% reserve)
        assert results.count(Acquire.BUDGET_EXHAUSTED) == 22
        assert lim.usage()["remaining"] == 100


class TestRegistry:
    def test_one_limiter_per_source_with_documented_spacing(self):
        assert get_limiter("theintrodb") is get_limiter("theintrodb")
        assert get_limiter("theintrodb").min_interval_s == pytest.approx(10 / 30 + 0.01)
        assert get_limiter("introdb").min_interval_s == 0.5
        assert get_limiter("skipdb").min_interval_s == 0.5

    def test_reset_limiters_forgets_instances(self):
        first = get_limiter("skipdb")
        reset_limiters()
        assert get_limiter("skipdb") is not first

    def test_a_limiter_created_after_a_restart_carries_on_from_todays_stored_usage(self, tmp_path, monkeypatch):
        # Audit B S8: before, a restart forgot the reserve (LOW allowed again) and the first response stored used=1.
        from media_preview_generator.markers.sources import ratelimit
        from media_preview_generator.markers.store import get_marker_store

        monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
        day = ratelimit._utc_day()
        get_marker_store().record_source_usage("theintrodb", day=day, used=80, limit=500, remaining=11)
        lim = get_limiter("theintrodb")
        assert lim.usage() == {
            "day": day,
            "used": 80,
            "limit": 500,
            "remaining": 11,
            "blocked_until_s": 0.0,
            "low_priority_exhausted": True,  # 11 <= the 20% reserve of 500
            "resets_at": ratelimit.RESET_TIME_LABEL,
        }
        assert lim.acquire(priority=ratelimit.PRIORITY_LOW) is Acquire.BUDGET_EXHAUSTED  # the 20% reserve holds
        assert lim.acquire(priority=2) is Acquire.ALLOWED
        lim.record(200, {"x-usagelimit-limit": "500", "x-usagelimit-remaining": "10"})
        assert get_marker_store().source_usage("theintrodb", day) == {"used": 81, "limit": 500, "remaining": 10}

    def test_usage_stored_on_another_day_is_not_carried_over(self, tmp_path, monkeypatch):
        from media_preview_generator.markers.store import get_marker_store

        monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
        get_marker_store().record_source_usage("theintrodb", day="2000-01-01", used=499, limit=500, remaining=1)
        lim = get_limiter("theintrodb")
        assert (lim.usage()["used"], lim.usage()["limit"], lim.usage()["remaining"]) == (0, None, None)

    def test_an_unreadable_store_leaves_a_fresh_limiter(self, monkeypatch, loguru_caplog):
        from media_preview_generator.markers import store as store_mod

        def broken(config_dir=None):
            raise OSError("read-only")

        monkeypatch.setattr(store_mod, "get_marker_store", broken)
        lim = get_limiter("skipdb")
        assert lim.usage()["used"] == 0
        assert lim.acquire(priority=3) is Acquire.ALLOWED
        assert "Could not read skipdb usage" in loguru_caplog.text

    @pytest.mark.parametrize(
        ("seed_day", "requests_first", "expected_used"),
        [("today", 0, 80), ("yesterday", 0, 0), ("today", 1, 1)],
        ids=["today", "other-day", "after-own-requests"],
    )
    def test_seed_usage_only_starts_a_fresh_count_for_today(self, seed_day, requests_first, expected_used):
        lim = SourceLimiter("theintrodb", min_interval_s=0.0, utc_day=lambda: "2026-09-14")
        for _ in range(requests_first):
            assert lim.acquire(priority=2) is Acquire.ALLOWED
        day = "2026-09-14" if seed_day == "today" else "2026-09-13"
        lim.seed_usage(day=day, used=80, limit=500, remaining=11)
        assert lim.usage()["used"] == expected_used

    def test_registered_limiter_persists_usage_to_the_marker_store(self, tmp_path, monkeypatch):
        from media_preview_generator.markers.store import get_marker_store

        monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
        lim = get_limiter("theintrodb")
        lim.record(200, {"x-usagelimit-limit": "500", "x-usagelimit-remaining": "417"})
        day = lim.usage()["day"]
        assert get_marker_store().source_usage("theintrodb", day) == {"used": 0, "limit": 500, "remaining": 417}
