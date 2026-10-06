"""Chapter attempts stay bounded even while a damaged demuxer prints errors."""

from __future__ import annotations

import signal
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from media_preview_generator.processing import ffmpeg_runner, generator


@pytest.fixture
def managed_process(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=0.0)
    state = SimpleNamespace(
        polls=0,
        paused=False,
        signals=[],
        killed_at=None,
        finished_after=80,
        stderr=None,
        output=lambda: "[matroska,webm] Invalid data found when processing input\n",
    )
    process = MagicMock(pid=4242, returncode=None)

    def poll():
        if process.returncode is not None:
            return process.returncode
        state.polls += 1
        clock.now += 0.25
        state.stderr.write(state.output())
        state.stderr.flush()
        if state.polls >= state.finished_after:
            process.returncode = 0
        return process.returncode

    def kill():
        state.killed_at = clock.now
        process.returncode = -9

    def terminate():
        process.returncode = -15

    def send_signal(value):
        state.signals.append(value)
        state.paused = value == signal.SIGSTOP

    def popen(_args, **kwargs):
        state.stderr = kwargs["stderr"]
        return process

    def sleep(seconds):
        clock.now += 10.0 if state.paused else seconds

    process.poll.side_effect = poll
    process.kill.side_effect = kill
    process.terminate.side_effect = terminate
    process.send_signal.side_effect = send_signal
    process.wait.side_effect = lambda **_kwargs: process.returncode
    monkeypatch.setattr(ffmpeg_runner.subprocess, "Popen", popen)
    monkeypatch.setattr(
        ffmpeg_runner,
        "time",
        SimpleNamespace(time=lambda: clock.now, monotonic=lambda: clock.now, time_ns=lambda: 1, sleep=sleep),
    )
    monkeypatch.setattr(generator, "_save_ffmpeg_failure_log", lambda *_args, **_kwargs: None)
    config = SimpleNamespace(
        ffmpeg_path="ffmpeg", ffmpeg_threads=1, thumbnail_quality=4, log_level="INFO", plex_bif_frame_interval=10
    )
    kwargs = dict(
        video_file="/synthetic/damaged.mkv",
        output_folder=str(tmp_path),
        gpu=None,
        gpu_device_path=None,
        config=config,
        progress_callback=None,
        ffmpeg_threads_override=1,
        cancel_check=None,
        pause_check=None,
        path_kind="sdr",
        libplacebo_vf=None,
        use_libplacebo=False,
        dv5_software_fallback=False,
        base_scale="scale=w=1280:h=-2",
        fps_filter="null",
        hdr10_zscale_chain="",
        chapter_start_ms=4000,
        chapter_output=str(tmp_path / "frame.jpg"),
    )
    return clock, state, process, kwargs


def test_noisy_demuxer_is_killed_and_reaped_at_total_active_deadline(managed_process):
    clock, state, process, kwargs = managed_process
    run = ffmpeg_runner.create_ffmpeg_runner(**kwargs, active_timeout_s=2.0)

    code, _elapsed, _speed, stderr = run(use_skip=False)

    assert code == -9
    assert ffmpeg_runner.ACTIVE_TIMEOUT_LINE in stderr
    assert ffmpeg_runner.STALL_WATCHDOG_LINE not in stderr
    assert any("Invalid data" in line for line in stderr)
    assert 2 <= state.killed_at < 3
    assert clock.now < 3
    process.kill.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=5)


def test_long_pause_does_not_consume_active_attempt_budget(managed_process):
    clock, state, process, kwargs = managed_process
    kwargs["pause_check"] = lambda: 2 <= state.polls <= 4
    run = ffmpeg_runner.create_ffmpeg_runner(**kwargs, active_timeout_s=2.0)

    code, _elapsed, _speed, stderr = run(use_skip=False)

    assert code == -9 and ffmpeg_runner.ACTIVE_TIMEOUT_LINE in stderr
    assert state.signals == [signal.SIGSTOP, signal.SIGCONT]
    assert 32 <= clock.now < 34
    assert not state.paused
    process.kill.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=5)


def test_cancellation_preempts_active_deadline_and_reaps_process(managed_process):
    _clock, state, process, kwargs = managed_process
    kwargs["cancel_check"] = lambda: state.polls >= 2
    run = ffmpeg_runner.create_ffmpeg_runner(**kwargs, active_timeout_s=2.0)

    with pytest.raises(generator.CancellationError):
        run(use_skip=False)

    process.terminate.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=5)
    process.kill.assert_not_called()


def test_success_before_deadline_keeps_normal_result(managed_process):
    _clock, state, process, kwargs = managed_process
    state.finished_after = 3
    run = ffmpeg_runner.create_ffmpeg_runner(**kwargs, active_timeout_s=2.0)

    code, _elapsed, _speed, stderr = run(use_skip=False)

    assert code == 0
    assert ffmpeg_runner.ACTIVE_TIMEOUT_LINE not in stderr
    process.kill.assert_not_called()


def test_existing_callers_without_budget_keep_their_stall_policy(managed_process):
    clock, _state, process, kwargs = managed_process
    run = ffmpeg_runner.create_ffmpeg_runner(**kwargs)

    code, _elapsed, _speed, stderr = run(use_skip=False)

    assert code == 0 and clock.now > 20
    assert ffmpeg_runner.ACTIVE_TIMEOUT_LINE not in stderr
    process.kill.assert_not_called()


@pytest.mark.parametrize("cancel", [False, True])
def test_uninterruptible_process_returns_explicit_failure_and_defers_reaping(managed_process, monkeypatch, cancel):
    _clock, state, process, kwargs = managed_process
    process.kill.side_effect = None
    process.terminate.side_effect = None
    process.wait.side_effect = subprocess.TimeoutExpired("ffmpeg", 5)
    thread = MagicMock()
    factory = MagicMock(return_value=thread)
    monkeypatch.setattr(ffmpeg_runner, "threading", SimpleNamespace(get_ident=lambda: 1, Thread=factory))
    if cancel:
        kwargs["cancel_check"] = lambda: state.polls >= 2
    run = ffmpeg_runner.create_ffmpeg_runner(**kwargs, active_timeout_s=2.0)

    if cancel:
        with pytest.raises(generator.CancellationError):
            run(use_skip=False)
        assert [call.kwargs for call in process.wait.call_args_list] == [{"timeout": 5}, {"timeout": 5}]
    else:
        code, _elapsed, _speed, stderr = run(use_skip=False)
        assert code == -signal.SIGKILL
        assert ffmpeg_runner.ACTIVE_TIMEOUT_LINE in stderr
        assert ffmpeg_runner.PROCESS_EXIT_PENDING_LINE in stderr
        process.wait.assert_called_once_with(timeout=5)

    assert process.returncode is None
    factory.assert_called_once_with(target=process.wait, name="ffmpeg-reap-4242", daemon=True)
    thread.start.assert_called_once_with()


def test_chapter_diagnostic_flood_terminates_with_bounded_reads_and_reaping(managed_process, monkeypatch):
    _clock, state, process, kwargs = managed_process
    state.output = lambda: "x" * (4 * 1024 * 1024 + 1)
    sizes = []
    real_open = open

    def tracked_open(path, mode="r", **options):
        handle = real_open(path, mode, **options)
        if mode != "r":
            return handle

        def read(size=-1):
            sizes.append(size)
            assert 0 < size <= 65536
            return handle.read(size)

        return SimpleNamespace(read=read, tell=handle.tell, seek=handle.seek, close=handle.close)

    monkeypatch.setattr(ffmpeg_runner, "open", tracked_open, raising=False)
    run = ffmpeg_runner.create_ffmpeg_runner(**kwargs, active_timeout_s=30.0)

    code, _elapsed, _speed, stderr = run(use_skip=False)

    assert code == -9 and ffmpeg_runner.DIAGNOSTIC_LIMIT_LINE in stderr
    assert sizes and len(stderr) <= 268 and max(map(len, stderr)) <= 8192
    process.kill.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=5)


def test_chapter_diagnostic_tail_preserves_earlier_fatal_corruption(managed_process):
    _clock, state, _process, kwargs = managed_process
    fatal = "[matroska,webm] 0x00 at pos 100 invalid as first byte of an EBML number"
    state.output = lambda: (fatal + "\n" if state.polls == 1 else "") + "noise\n" * 600
    state.finished_after = 3
    run = ffmpeg_runner.create_ffmpeg_runner(**kwargs, active_timeout_s=30.0)

    code, _elapsed, _speed, stderr = run(use_skip=False)

    assert code == 0 and fatal in stderr
    assert len(stderr) <= 268 and max(map(len, stderr)) <= 8192
    from media_preview_generator.processing.chapters import ChapterSourceCorruptionError, _check_fatal_extraction

    with pytest.raises(ChapterSourceCorruptionError):
        _check_fatal_extraction(code, stderr, 4000)


def test_success_with_large_unread_diagnostics_is_not_accepted(managed_process):
    _clock, state, process, kwargs = managed_process
    state.output = lambda: "unexamined output\n" * 100000
    state.finished_after = 1
    run = ffmpeg_runner.create_ffmpeg_runner(**kwargs, active_timeout_s=30.0)

    code, _elapsed, _speed, stderr = run(use_skip=False)

    assert code == 0 and ffmpeg_runner.DIAGNOSTIC_LIMIT_LINE in stderr
    assert len(stderr) <= 268 and max(map(len, stderr)) <= 8192
    process.kill.assert_not_called()
    from media_preview_generator.processing.chapters import ChapterExtractionStalledError, _check_fatal_extraction

    with pytest.raises(ChapterExtractionStalledError):
        _check_fatal_extraction(code, stderr, 4000)
