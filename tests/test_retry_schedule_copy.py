"""The settings page's retry copy must describe the schedule the job runner uses.

The "Initial retry delay" slider once claimed the first wait equalled the
slider value and that each later wait doubled.  The job runner actually
walks ``BACKOFF_SCHEDULE`` (1 min, 2 min, 5 min, 15 min, 1 h) and scales
every step by ``slider ÷ 30``.  These tests pin the scaling rule and tie
the copy to it, so the two can't drift apart again.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from media_preview_generator.processing.retry_queue import BACKOFF_SCHEDULE, scaled_backoff_delay

SETTINGS_HTML = Path(__file__).resolve().parent.parent / "media_preview_generator/web/templates/settings.html"


def _retry_delay_copy() -> str:
    """Return the retry-delay label, tooltip and hint text from settings.html."""
    text = SETTINGS_HTML.read_text(encoding="utf-8")
    match = re.search(r'<label for="webhookRetryDelay".*?<div class="form-text">.*?</div>', text, re.DOTALL)
    assert match, "settings.html: retry-delay block not found — update the scope regex"
    return " ".join(match.group(0).split())


def _as_words(seconds: int) -> str:
    """Render a schedule step the way the copy spells it (``60`` → ``1 min``)."""
    if seconds % 3600 == 0:
        return f"{seconds // 3600} h"
    return f"{seconds // 60} min"


class TestScaledBackoffDelay:
    def test_default_setting_walks_the_schedule_when_delay_is_30(self) -> None:
        delays = [scaled_backoff_delay(attempt, 30) for attempt in range(1, len(BACKOFF_SCHEDULE) + 1)]
        assert delays == list(BACKOFF_SCHEDULE)

    @pytest.mark.parametrize(("retry_delay", "factor"), [(60, 2), (90, 3), (300, 10)])
    def test_every_step_scales_when_delay_is_above_30(self, retry_delay: int, factor: int) -> None:
        delays = [scaled_backoff_delay(attempt, retry_delay) for attempt in range(1, len(BACKOFF_SCHEDULE) + 1)]
        assert delays == [step * factor for step in BACKOFF_SCHEDULE]

    def test_steps_halve_when_delay_is_15(self) -> None:
        assert scaled_backoff_delay(1, 15) == BACKOFF_SCHEDULE[0] // 2

    @pytest.mark.parametrize("retry_delay", [10, 5, 0])
    def test_scale_is_floored_at_half_when_delay_is_below_15(self, retry_delay: int) -> None:
        assert scaled_backoff_delay(1, retry_delay) == scaled_backoff_delay(1, 15)

    @pytest.mark.parametrize("attempt", [len(BACKOFF_SCHEDULE) + 1, 10])
    def test_last_step_repeats_when_attempt_is_past_the_schedule(self, attempt: int) -> None:
        assert scaled_backoff_delay(attempt, 30) == BACKOFF_SCHEDULE[-1]


class TestSettingsRetryCopy:
    def test_copy_never_claims_waits_double_when_rendered(self) -> None:
        copy = _retry_delay_copy().lower()
        for tell in ("double the wait", "twice as long", "doubles the wait", "capped at the slider"):
            assert tell not in copy, f"settings.html still describes a doubling retry schedule: {tell!r}"

    def test_copy_lists_every_default_wait_when_rendered(self) -> None:
        copy = _retry_delay_copy()
        for step in BACKOFF_SCHEDULE:
            assert _as_words(step) in copy, f"settings.html retry copy omits the {_as_words(step)} step"

    def test_copy_states_the_scaling_example_when_rendered(self) -> None:
        # The copy promises "60 s doubles every wait"; hold the code to it.
        assert scaled_backoff_delay(1, 60) == 2 * scaled_backoff_delay(1, 30)
        assert "60 s doubles" in _retry_delay_copy()

    def test_copy_states_the_floor_when_rendered(self) -> None:
        assert scaled_backoff_delay(1, 10) == scaled_backoff_delay(1, 15)
        assert "under 15 s" in _retry_delay_copy()
