"""An FFmpeg the app's shutdown (or a job cancel) stopped is logged as a stop, not a crash, and isn't retried.

On a restart (2026-09-24 05:32:49) s6 sent SIGTERM to every process; FFmpeg printed "Exiting normally, received signal
15." and exited 255, which was logged as ERROR "FFmpeg failed" / "crashed with an unusual exit code" and started the
full-frame retry. A real crash (same exit code, no shutdown, no cancel) must still be handled as before.
"""

from __future__ import annotations

import signal
import threading
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger

from media_preview_generator import shutdown
from media_preview_generator.processing import ffmpeg_runner
from media_preview_generator.processing.generator import CancellationError, generate_images

VIDEO = "/data/tv/CSI (2000)/Season 04/CSI (2000) - S04E20 - Dead Ringer.mkv"
SIGTERM_TAIL = [
    "frame=  206 fps= 21 q=4.0 size=N/A time=00:20:35.99 bitrate=N/A speed= 124x elapsed=0:00:10.00",
    "[out#0/image2 @ 0x5bfedb3db280] video:1428KiB audio:0KiB subtitle:0KiB other streams:0KiB",
    "Exiting normally, received signal 15.",
]
CRASH_TAIL = ["[h264 @ 0x55] Invalid NAL unit size (1234 > 567).", "Error while decoding stream #0:0"]


@pytest.fixture(autouse=True)
def _not_shutting_down():
    shutdown.reset_for_tests()
    yield
    shutdown.reset_for_tests()


@pytest.fixture
def logs():
    lines: list[tuple[str, str]] = []
    handler_id = logger.add(lambda m: lines.append((m.record["level"].name, m.record["message"])), level="DEBUG")
    yield lines
    logger.remove(handler_id)


def _run(temp_dir, mock_config, stderr_tail, returncode, *, cancel_check=None, on_exit=None):
    """Run generate_images with an FFmpeg that writes ``stderr_tail`` and exits ``returncode`` on every attempt."""
    procs = []

    def fake_popen(args, stderr=None, stdout=None, env=None):
        stderr.write("\n".join(stderr_tail) + "\n")
        stderr.flush()
        proc = MagicMock(returncode=returncode)
        proc.poll.return_value = returncode
        procs.append(proc)
        if on_exit is not None:
            on_exit()
        return proc

    info = MagicMock()
    info.video_tracks = [MagicMock(hdr_format=None)]
    with (
        patch("media_preview_generator.processing.generator.MediaInfo") as media_info,
        patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")),
        patch("subprocess.Popen", side_effect=fake_popen),
        patch("media_preview_generator.processing.generator._probe_max_keyframe_gap", return_value=1.0),
        patch("media_preview_generator.processing.generator._save_ffmpeg_failure_log") as failure_log,
    ):
        media_info.parse.return_value = info
        mock_config.plex_bif_frame_interval = 5
        outcome = None
        try:
            outcome = generate_images(VIDEO, temp_dir, None, None, mock_config, cancel_check=cancel_check)
        except CancellationError as exc:
            outcome = exc
    return outcome, procs, failure_log


def _messages(logs, level):
    return [message for lvl, message in logs if lvl == level]


class TestStoppedForShutdown:
    def test_ffmpeg_killed_while_the_app_shuts_down_is_logged_as_a_stop_and_not_retried(
        self, temp_dir, mock_config, logs
    ):
        shutdown.mark_shutting_down()

        outcome, procs, failure_log = _run(temp_dir, mock_config, SIGTERM_TAIL, 255)

        assert isinstance(outcome, CancellationError)
        assert len(procs) == 1, "no full-frame retry after a shutdown stop"
        assert _messages(logs, "ERROR") == []
        info = [m for m in _messages(logs, "INFO") if m.startswith("FFmpeg stopped for shutdown")]
        assert info == [
            f"FFmpeg stopped for shutdown while processing {VIDEO} (exit code 255); not a crash, so it isn't "
            "retried now"
        ]
        assert not any("crashed" in m or "full-frame" in m for m in _messages(logs, "WARNING"))
        failure_log.assert_not_called()

    def test_ffmpeg_that_exits_just_before_the_app_marks_its_shutdown_is_still_a_stop(
        self, temp_dir, mock_config, logs
    ):
        """The container stop signals FFmpeg and the app together; FFmpeg's exit can win the race."""
        outcome, procs, _ = _run(
            temp_dir,
            mock_config,
            SIGTERM_TAIL,
            255,
            on_exit=lambda: threading.Timer(0.05, shutdown.mark_shutting_down).start(),
        )

        assert isinstance(outcome, CancellationError)
        assert len(procs) == 1
        assert _messages(logs, "ERROR") == []

    @pytest.mark.parametrize("returncode", [-signal.SIGTERM, 128 + signal.SIGTERM, -signal.SIGINT])
    def test_ffmpeg_killed_by_the_signal_itself_is_a_stop_too(self, temp_dir, mock_config, logs, returncode):
        shutdown.mark_shutting_down()
        outcome, procs, _ = _run(temp_dir, mock_config, SIGTERM_TAIL[:2], returncode)
        assert isinstance(outcome, CancellationError)
        assert len(procs) == 1
        assert not any("killed by the operating system" in m for m in _messages(logs, "WARNING"))


class TestStoppedForCancel:
    def test_ffmpeg_that_failed_as_the_job_was_cancelled_is_logged_as_a_stop(self, temp_dir, mock_config, logs):
        cancelled = {"now": False}

        outcome, procs, failure_log = _run(
            temp_dir,
            mock_config,
            SIGTERM_TAIL,
            255,
            cancel_check=lambda: cancelled["now"],
            on_exit=lambda: cancelled.update(now=True),
        )

        assert isinstance(outcome, CancellationError)
        assert len(procs) == 1
        assert _messages(logs, "ERROR") == []
        assert any(m.startswith("FFmpeg stopped for cancellation while processing") for m in _messages(logs, "INFO"))
        failure_log.assert_not_called()


class TestRealCrashUnchanged:
    def test_a_crash_with_no_shutdown_or_cancel_is_logged_and_retried_as_before(self, temp_dir, mock_config, logs):
        with patch.object(ffmpeg_runner, "wait_for_shutdown") as wait:
            outcome, procs, failure_log = _run(temp_dir, mock_config, CRASH_TAIL, 255)

        wait.assert_not_called()  # no signal in its stderr, so no wait for a shutdown
        assert outcome[0] is False, "no frames, the file failed"
        assert len(procs) == 2, "the full-frame retry still runs"
        errors = _messages(logs, "ERROR")
        assert any(m.startswith(f"FFmpeg failed while extracting frames from {VIDEO} (exit code 255") for m in errors)
        assert any("crashed with an unusual exit code (255)" in m for m in _messages(logs, "WARNING"))
        assert any("retrying with full-frame decode" in m for m in _messages(logs, "WARNING"))
        assert failure_log.call_count == 2

    def test_a_signalled_ffmpeg_outside_a_shutdown_is_still_a_failure(self, temp_dir, mock_config, logs, monkeypatch):
        """Someone killed FFmpeg by hand: after the short wait for a shutdown that never comes, it's handled as a
        failure, as before."""
        monkeypatch.setattr(ffmpeg_runner, "SHUTDOWN_SIGNAL_GRACE_S", 0.01)

        outcome, procs, _ = _run(temp_dir, mock_config, SIGTERM_TAIL, 255)

        assert outcome[0] is False
        assert len(procs) == 2
        assert any(m.startswith("FFmpeg failed while extracting frames") for m in _messages(logs, "ERROR"))


class TestShutdownSignalHandlers:
    def test_sigterm_marks_the_shutdown_then_runs_the_previous_handler(self, monkeypatch):
        previous = MagicMock()
        installed: dict[int, object] = {}
        monkeypatch.setattr(shutdown, "_installed", False)
        monkeypatch.setattr(
            shutdown.signal, "getsignal", lambda signum: previous if signum == signal.SIGTERM else signal.SIG_IGN
        )
        monkeypatch.setattr(shutdown.signal, "signal", lambda signum, handler: installed.__setitem__(signum, handler))
        monkeypatch.setattr(shutdown.signal, "siginterrupt", MagicMock())
        monkeypatch.setattr(shutdown.atexit, "register", MagicMock())

        shutdown.install_signal_handlers()
        installed[signal.SIGTERM](signal.SIGTERM, None)

        assert shutdown.is_shutting_down()
        previous.assert_called_once_with(signal.SIGTERM, None)
        shutdown.signal.siginterrupt.assert_called_once_with(signal.SIGTERM, False)

    def test_install_is_once_per_process_and_main_thread_only(self, monkeypatch):
        calls = []
        monkeypatch.setattr(shutdown, "_installed", False)
        monkeypatch.setattr(shutdown.signal, "signal", lambda signum, handler: calls.append(signum))
        monkeypatch.setattr(shutdown.signal, "siginterrupt", MagicMock())
        monkeypatch.setattr(shutdown.atexit, "register", MagicMock())

        worker = threading.Thread(target=shutdown.install_signal_handlers)
        worker.start()
        worker.join()
        assert calls == []

        shutdown.install_signal_handlers()
        shutdown.install_signal_handlers()
        assert calls == [signal.SIGTERM, signal.SIGINT]
