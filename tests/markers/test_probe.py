"""Tests for media_preview_generator.markers.probe (spec §5.1)."""

import json
import os
import signal
import subprocess
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.markers import probe
from media_preview_generator.markers.probe import (
    Chapter,
    ProbeError,
    ProbeStalledError,
    ProbeTimeoutError,
    ffprobe_path_for,
    kill_and_collect,
    probe_media,
)

RUN = "media_preview_generator.markers.probe.subprocess.Popen"


@pytest.fixture(autouse=True)
def _no_stuck_ffprobes():
    # The count is process-wide: one leaked from a test would stop the next test's ffprobe starting.
    assert probe.stuck_processes(probe.FFPROBE_REAPER) == 0
    yield
    assert probe.stuck_processes(probe.FFPROBE_REAPER) == 0


def _proc(returncode=0, stdout="", stderr=""):
    """A finished ffprobe as ``Popen`` hands it back."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate.return_value = (stdout, stderr)
    return proc


def _ok(payload):
    return _proc(stdout=json.dumps(payload))


def test_parses_duration_and_chapters_and_passes_exact_args():
    payload = {
        "format": {"duration": "1321.472000"},
        "chapters": [
            {"start_time": "0.000000", "end_time": "127.961000", "tags": {"title": "Chapter 1"}},
            {"start_time": "127.961000", "end_time": "159.826000", "tags": {"TITLE": "Title Sequence"}},
            {"start_time": "159.826000", "end_time": "1321.472000", "tags": {}},
        ],
    }
    proc = _ok(payload)
    with patch(RUN, return_value=proc) as run:
        probe = probe_media("/m/a.mkv", ffprobe="/usr/bin/ffprobe")
    args, kwargs = run.call_args
    assert args[0] == [
        "/usr/bin/ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_chapters",
        "/m/a.mkv",
    ]
    assert kwargs == {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True}
    assert proc.communicate.call_args.kwargs == {"timeout": 60.0}
    assert probe.duration_ms == 1_321_472
    assert probe.chapters == (
        Chapter(0, 127_961, "Chapter 1"),
        Chapter(127_961, 159_826, "Title Sequence"),
        Chapter(159_826, 1_321_472, ""),
    )


@pytest.mark.parametrize(
    ("fmt", "expected"),
    [
        ({"start_time": "30000.000000"}, 30_000_000),  # a recorded-TV .ts with a PCR base
        ({"start_time": "0.000000"}, 0),
        ({"start_time": "-0.023000"}, -23),
        ({}, None),
        ({"start_time": "N/A"}, None),
    ],
)
def test_container_start_time(fmt, expected):
    # Credits timestamps are read with -copyts, so the container's own start has to be subtracted from them.
    with patch(RUN, return_value=_ok({"format": fmt, "chapters": []})):
        assert probe_media("/m/a.ts", ffprobe="ffprobe").start_time_ms == expected


def test_missing_duration_is_none():
    with patch(RUN, return_value=_ok({"format": {}, "chapters": []})):
        assert probe_media("/m/a.mkv", ffprobe="ffprobe").duration_ms is None


@pytest.mark.parametrize(
    "side_effect",
    [
        FileNotFoundError("ffprobe"),
        PermissionError("ffprobe"),
        None,  # non-zero return code
    ],
)
def test_failures_raise_probe_error(side_effect):
    ret = _proc(returncode=1, stderr="Invalid data found when processing input")
    with patch(RUN, side_effect=side_effect, return_value=ret):
        with pytest.raises(ProbeError) as caught:
            probe_media("/m/a.mkv", ffprobe="ffprobe")
    assert type(caught.value) is ProbeError  # not a timeout: the credit text path doesn't back off for a day


def test_a_timeout_kills_ffprobe_and_is_a_probe_timeout():
    proc = _proc()
    proc.communicate.side_effect = [subprocess.TimeoutExpired(cmd="ffprobe", timeout=60), ("", "")]
    with patch(RUN, return_value=proc):
        with pytest.raises(ProbeTimeoutError, match="ffprobe failed for /m/a.mkv: TimeoutExpired"):
            probe_media("/m/a.mkv", ffprobe="ffprobe")
    proc.kill.assert_called_once_with()
    # Collected with a bounded wait, never subprocess.run's unbounded one.
    assert proc.communicate.call_args.kwargs == {"timeout": probe.KILL_WAIT_S}


@pytest.mark.parametrize("holds_output", [False, True], ids=["exits-when-killed", "output-outlives-the-kill"])
def test_a_stalled_ffprobe_never_holds_its_caller(tmp_path, monkeypatch, holds_output):
    # A read stalled on a hard network mount leaves ffprobe unkillable and its pipes open for as long as the stall
    # lasts; here a process in a session of its own keeps them open after ffprobe is killed, the same shape.
    monkeypatch.setattr(probe, "KILL_WAIT_S", 0.5)
    pid_file = tmp_path / "holder.pid"
    holder = f'setsid sleep 60 &\necho $! > "{pid_file}"\n' if holds_output else ""
    script = tmp_path / "ffprobe"
    script.write_text(f"#!/bin/sh\n{holder}exec sleep 60\n")
    script.chmod(0o755)
    started: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def popen(*args, **kwargs):
        started.append(real_popen(*args, **kwargs))
        return started[-1]

    monkeypatch.setattr("media_preview_generator.markers.probe.subprocess.Popen", popen)
    began = time.monotonic()
    try:
        with pytest.raises(ProbeTimeoutError):
            probe_media(str(tmp_path / "a.mkv"), ffprobe=str(script), timeout_s=0.5)
        elapsed = time.monotonic() - began
        (proc,) = started
        assert (proc.returncode is None) is holds_output  # handed to the reaper, or collected here
    finally:
        if holds_output:
            for _ in range(50):
                if pid_file.exists() and pid_file.read_text().strip():
                    break
                time.sleep(0.1)
            os.kill(int(pid_file.read_text()), signal.SIGKILL)
    assert elapsed < 0.5 + 0.5 + 3.0  # bounded, not the 60 s the pipe is held
    for reaper in [t for t in threading.enumerate() if t.name == "ffprobe-reaper"]:
        reaper.join(10)
    assert proc.returncode == -signal.SIGKILL  # the reaper collects it once it lets go: no zombie


def test_invalid_json_raises():
    with patch(RUN, return_value=_proc(stdout="not json")):
        with pytest.raises(ProbeError):
            probe_media("/m/a.mkv", ffprobe="ffprobe")


def test_ffprobe_path_prefers_sibling_of_ffmpeg(tmp_path):
    ffmpeg = tmp_path / "ffmpeg"
    ffprobe = tmp_path / "ffprobe"
    ffmpeg.write_text("")
    ffprobe.write_text("")
    ffprobe.chmod(0o755)
    assert ffprobe_path_for(str(ffmpeg)) == str(ffprobe)


def test_ffprobe_path_falls_back_to_path_lookup(tmp_path):
    with patch("media_preview_generator.markers.probe.shutil.which", return_value="/usr/bin/ffprobe"):
        assert ffprobe_path_for(str(tmp_path / "ffmpeg")) == "/usr/bin/ffprobe"
        assert ffprobe_path_for(None) == "/usr/bin/ffprobe"


def test_ffprobe_path_skips_non_executable_sibling(tmp_path):
    ffmpeg = tmp_path / "ffmpeg"
    ffprobe = tmp_path / "ffprobe"
    ffmpeg.write_text("")
    ffprobe.write_text("")
    ffprobe.chmod(0o644)  # a sibling file that exists but isn't runnable must not be trusted
    with patch("media_preview_generator.markers.probe.shutil.which", return_value="/usr/bin/ffprobe"):
        assert ffprobe_path_for(str(ffmpeg)) == "/usr/bin/ffprobe"


def test_ms_rounds_to_nearest_instead_of_truncating():
    # 1.0009 * 1000 = 1000.9: round -> 1001, truncation -> 1000. Ties matter for BIF timestamps.
    with patch(RUN, return_value=_ok({"format": {"duration": "1.0009"}, "chapters": []})):
        assert probe_media("/m/a.mkv", ffprobe="ffprobe").duration_ms == 1001


def test_nonzero_returncode_raises_even_when_stdout_is_valid_json():
    payload = json.dumps({"format": {}, "chapters": []})
    ret = _proc(returncode=1, stdout=payload, stderr="Invalid data found when processing input")
    with patch(RUN, return_value=ret):
        with pytest.raises(ProbeError):
            probe_media("/m/a.mkv", ffprobe="ffprobe")


def test_nonzero_returncode_error_message_includes_path():
    ret = _proc(returncode=1, stderr="broken")
    with patch(RUN, return_value=ret):
        with pytest.raises(ProbeError, match=r"/m/a\.mkv"):
            probe_media("/m/a.mkv", ffprobe="ffprobe")


def test_probe_error_message_truncates_huge_stderr():
    huge = "x" * 5000
    ret = _proc(returncode=1, stderr=huge)
    with patch(RUN, return_value=ret):
        with pytest.raises(ProbeError) as exc_info:
            probe_media("/m/a.mkv", ffprobe="ffprobe")
    assert huge not in str(exc_info.value)
    assert len(str(exc_info.value)) < 400


def test_timeout_s_is_passed_through_verbatim():
    proc = _ok({"format": {}, "chapters": []})
    with patch(RUN, return_value=proc):
        probe_media("/m/a.mkv", ffprobe="ffprobe", timeout_s=5)
    assert proc.communicate.call_args.kwargs == {"timeout": 5}


def test_duration_non_numeric_string_is_none():
    with patch(RUN, return_value=_ok({"format": {"duration": "abc"}, "chapters": []})):
        assert probe_media("/m/a.mkv", ffprobe="ffprobe").duration_ms is None


def test_duration_inf_is_none():
    with patch(RUN, return_value=_ok({"format": {"duration": "inf"}, "chapters": []})):
        assert probe_media("/m/a.mkv", ffprobe="ffprobe").duration_ms is None


def test_non_dict_json_raises():
    with patch(RUN, return_value=_proc(stdout="[]")):
        with pytest.raises(ProbeError):
            probe_media("/m/a.mkv", ffprobe="ffprobe")


def test_chapters_key_absent_defaults_to_no_chapters():
    with patch(RUN, return_value=_ok({"format": {"duration": "10.0"}})):
        probe = probe_media("/m/a.mkv", ffprobe="ffprobe")
    assert probe.chapters == ()


def test_format_key_absent_duration_is_none():
    with patch(RUN, return_value=_ok({"chapters": []})):
        assert probe_media("/m/a.mkv", ffprobe="ffprobe").duration_ms is None


def test_chapter_with_missing_start_time_is_skipped():
    payload = {"format": {"duration": "10.0"}, "chapters": [{"end_time": "5.0", "tags": {"title": "Bad"}}]}
    with patch(RUN, return_value=_ok(payload)):
        probe = probe_media("/m/a.mkv", ffprobe="ffprobe")
    assert probe.chapters == ()


def test_ffprobe_path_returns_literal_when_which_finds_nothing():
    with patch("media_preview_generator.markers.probe.shutil.which", return_value=None):
        assert ffprobe_path_for(None) == "ffprobe"


def test_ffprobe_path_skips_directory_named_ffprobe(tmp_path):
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_text("")
    (tmp_path / "ffprobe").mkdir()  # a directory, not a binary -- os.access(X_OK) alone would pass this
    with patch("media_preview_generator.markers.probe.shutil.which", return_value="/usr/bin/ffprobe"):
        assert ffprobe_path_for(str(ffmpeg)) == "/usr/bin/ffprobe"


class _Proc:
    """A killed process that lets go of its pipes only once ``released`` is set."""

    def __init__(self, released: bool):
        self.released = threading.Event()
        if released:
            self.released.set()
        self.killed = False
        self.waits: list[float | None] = []

    def kill(self):
        self.killed = True

    def communicate(self, timeout=None):
        self.waits.append(timeout)
        if not self.released.wait(timeout):
            raise subprocess.TimeoutExpired("ffprobe", timeout)
        return "", ""


def test_a_process_that_exits_when_killed_is_collected_here(loguru_caplog):
    proc = _Proc(released=True)
    assert kill_and_collect(proc, what="ffprobe reading a.mkv", reaper_name="test-reaper") is True
    assert proc.killed and proc.waits == [probe.KILL_WAIT_S]
    assert probe.stuck_processes("test-reaper") == 0  # never counted: nothing was handed on
    assert "still holds its output" not in loguru_caplog.text


def _join_reapers(name):
    for reaper in [t for t in threading.enumerate() if t.name == name]:
        reaper.join(5)


def test_a_process_that_holds_its_output_is_counted_until_a_reaper_collects_it(loguru_caplog):
    proc = _Proc(released=False)
    assert kill_and_collect(proc, what="ffprobe reading a.mkv", reaper_name="test-reaper", wait_s=0.05) is False
    assert proc.killed and proc.waits[0] == 0.05
    assert "ffprobe reading a.mkv still holds its output after being stopped" in loguru_caplog.text
    time.sleep(0.2)
    assert probe.stuck_processes("test-reaper") == 1  # still stuck: not collected yet
    assert probe.stuck_processes("other-reaper") == 0  # counted per reaper
    proc.released.set()  # the stalled read returns
    _join_reapers("test-reaper")
    assert probe.stuck_processes("test-reaper") == 0
    assert proc.waits[-1] is None  # the reaper, and only the reaper, waits without a limit


def test_a_reaper_that_cant_start_leaves_nothing_counted(loguru_caplog, monkeypatch):
    # Nothing would ever collect the process, so nothing could count it down: counted, it would block that kind of
    # process for the rest of the app's run.
    def no_thread(*args, **kwargs):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(probe.threading.Thread, "start", no_thread)
    proc = _Proc(released=False)
    assert kill_and_collect(proc, what="ffprobe reading a.mkv", reaper_name="test-reaper", wait_s=0.05) is False
    assert probe.stuck_processes("test-reaper") == 0
    assert "Couldn't start a thread to collect ffprobe reading a.mkv: can't start new thread" in loguru_caplog.text


def test_two_stuck_ffprobes_keep_a_third_from_starting(monkeypatch):
    # A mount that answers stat but not reads: each timed-out ffprobe hands its caller back and stays stuck. Without a
    # limit every caller (a check thread per file, a season member, a worker's start-time probe) would leave another.
    monkeypatch.setattr(probe, "KILL_WAIT_S", 0.05)
    started: list[_Proc] = []

    def popen(*args, **kwargs):
        started.append(_Proc(released=False))
        return started[-1]

    monkeypatch.setattr("media_preview_generator.markers.probe.subprocess.Popen", popen)
    try:
        for name in ("a.mkv", "b.mkv"):
            with pytest.raises(ProbeTimeoutError):
                probe_media(f"/m/{name}", ffprobe="ffprobe", timeout_s=0.05)
        assert probe.stuck_processes(probe.FFPROBE_REAPER) == 2
        with pytest.raises(ProbeStalledError, match="2 earlier ffprobes are still stuck") as caught:
            probe_media("/m/c.mkv", ffprobe="ffprobe", timeout_s=0.05)
        assert isinstance(caught.value, ProbeError) and not isinstance(caught.value, ProbeTimeoutError)
        assert len(started) == 2  # no ffprobe started for the third file
        started[0].released.set()  # one stall ends
        deadline = time.monotonic() + 5
        while probe.stuck_processes(probe.FFPROBE_REAPER) != 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        monkeypatch.setattr("media_preview_generator.markers.probe.subprocess.Popen", lambda *a, **k: _ok({}))
        assert probe_media("/m/c.mkv", ffprobe="ffprobe").duration_ms is None  # room again: it runs
    finally:
        for proc in started:
            proc.released.set()
        _join_reapers(probe.FFPROBE_REAPER)
