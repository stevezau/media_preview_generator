"""StepProgress: one FFmpeg step's percent, speed and ETA for the worker row."""

from __future__ import annotations

import io
from unittest.mock import MagicMock

from media_preview_generator.markers.progress import StepProgress, read_progress


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_percent_is_clamped_to_the_steps_length():
    callback = MagicMock()
    step = StepProgress(callback, total_s=60.0, clock=_Clock())
    step.update(30.0)
    step.update(90.0)
    step.update(-5.0)
    assert [call.args[0] for call in callback.call_args_list] == [50.0, 100.0, 0.0]
    assert callback.call_args_list[1].args[1:3] == (60.0, 60.0)


def test_speed_and_remaining_are_computed_from_the_clock():
    clock, callback = _Clock(), MagicMock()
    step = StepProgress(callback, total_s=100.0, clock=clock)
    step.update(10.0)
    assert callback.call_args.args[3:] == (None, None)  # too early for a speed
    clock.now += 5.0
    step.update(20.0)
    percent, done, total, speed, remaining = callback.call_args.args
    assert (percent, done, total, speed) == (20.0, 20.0, 100.0, "4.0x")
    assert remaining == 20.0  # 80 s left at 4 media seconds per second


def test_speed_comes_from_the_pause_aware_clock_not_ffmpegs():
    clock, callback = _Clock(), MagicMock()
    step = StepProgress(callback, total_s=100.0, clock=clock)
    clock.now += 10.0  # the clock leaves paused time out, so only 10 s of running time passed
    step.update(20.0)
    assert callback.call_args.args[3:] == ("2.0x", 40.0)


def test_a_failing_callback_is_swallowed_and_logged_once(monkeypatch):
    from media_preview_generator.markers import progress

    warnings = MagicMock()
    monkeypatch.setattr(progress.logger, "warning", warnings)
    step = StepProgress(MagicMock(side_effect=RuntimeError("boom")), total_s=10.0, clock=_Clock())
    step.update(1.0)
    step.update(2.0)
    assert warnings.call_count == 1


def test_maybe_gives_nothing_without_a_callback_or_a_length():
    assert StepProgress.maybe(None, 10.0) is None
    assert StepProgress.maybe(MagicMock(), 0.0) is None
    assert StepProgress.maybe(MagicMock(), None) is None
    assert StepProgress.maybe(MagicMock(), 10.0) is not None


def test_read_progress_reports_each_block_and_survives_a_failing_callback():
    seen = []
    read_progress(io.BytesIO(b"out_time_us=2000000\nspeed=1.4x\nprogress=continue\n"), lambda s, x: seen.append((s, x)))
    assert seen == [(2.0, 1.4)]
