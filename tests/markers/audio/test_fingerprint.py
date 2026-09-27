"""Chromaprint fingerprints: window, command, ffmpeg choice, caching, identity gate, cancel, pause, no app-wide cap."""

from __future__ import annotations

import errno
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from media_preview_generator.markers.audio import POINT_S
from media_preview_generator.markers.audio import fingerprint as fpmod
from media_preview_generator.markers.models import FileIdentity
from media_preview_generator.markers.store import MarkerStore


@pytest.fixture(autouse=True)
def _fresh_chromaprint_cache():
    fpmod.forget_chromaprint_answers()
    yield
    fpmod.forget_chromaprint_answers()


class _Clock:
    def __init__(self, now: float = 1_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def store(tmp_path):
    s = MarkerStore(str(tmp_path / "markers.db"))
    yield s
    s.close()


def _proc(stdout=b"", stderr=b"", returncode=0, hang=False):
    proc = MagicMock()
    proc.returncode = returncode
    calls = {"n": 0}

    def communicate(timeout=None):
        calls["n"] += 1
        if hang and proc.kill.call_count == 0:
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        return stdout, stderr

    proc.communicate.side_effect = communicate
    return proc


def test_point_duration_is_the_measured_value():
    assert POINT_S == pytest.approx(0.12384, abs=1e-5)


@pytest.mark.parametrize(
    ("duration_ms", "expected"), [(1_321_472, 462.5152), (2_400_000, 840.0), (3_000_000, 900.0), (120_000, 42.0)]
)
def test_window_is_35_percent_capped_at_900_s(duration_ms, expected):
    assert fpmod.window_s(duration_ms) == pytest.approx(expected)


def test_command_is_the_spec_command_with_ffmpegs_own_thread_count_on_a_cpu_worker():
    # A CPU worker's ffmpeg is never capped, as previews' CPU decode isn't.
    assert fpmod.fingerprint_command("/usr/lib/jellyfin-ffmpeg/ffmpeg", "/m/a.mkv", 462.5152) == [
        "/usr/lib/jellyfin-ffmpeg/ffmpeg", "-nostdin", "-v", "error",
        "-ss", "0", "-t", "462.515", "-i", "/m/a.mkv",
        "-vn", "-sn", "-dn", "-ac", "2", "-f", "chromaprint", "-algorithm", "1", "-fp_format", "raw", "-",
    ]  # fmt: skip


def test_a_fingerprint_runs_at_ffmpegs_own_thread_count():
    # Chromaprint is CPU work even on a GPU worker: no GPU's ffmpeg_threads caps it, as none caps previews' CPU work.
    command = fpmod.fingerprint_command("ffmpeg", "/m/a.mkv", 462.5152)
    assert "-threads" not in command and "-filter_threads" not in command


@pytest.mark.parametrize(
    ("retime", "rate"),
    [(24000 / 1001 / 25, 46034), (25 / (24000 / 1001), 50050)],
    ids=["pal-slowed-to-film", "film-sped-to-pal"],
)
def test_a_retimed_command_plays_the_audio_at_the_groups_speed(retime, rate):
    # Resampled to 48 kHz first, so the declared rate is the same speed change whatever rate the file has.
    assert fpmod.fingerprint_command("ffmpeg", "/m/a.mkv", 462.5152, retime=retime) == [
        "ffmpeg", "-nostdin", "-v", "error",
        "-ss", "0", "-t", "462.515", "-i", "/m/a.mkv",
        "-vn", "-sn", "-dn", "-ac", "2", "-af", f"aresample=48000,asetrate={rate}",
        "-f", "chromaprint", "-algorithm", "1", "-fp_format", "raw", "-",
    ]  # fmt: skip


@pytest.mark.parametrize("retime", [float("nan"), float("inf"), -0.959, 0.0, 0.5, 2.0, 3.0], ids=str)
def test_a_retime_outside_a_plausible_speed_change_starts_no_ffmpeg(retime):
    with patch.object(fpmod.subprocess, "Popen") as popen, pytest.raises(fpmod.FingerprintError, match="retime"):
        fpmod.compute_fingerprint("/m/a.mkv", 1_321_472, ffmpeg="ffmpeg", retime=retime)
    popen.assert_not_called()


def test_each_retime_has_a_window_of_its_own():
    assert fpmod.fingerprint_window(None) == fpmod.WINDOW == "intro"
    assert fpmod.fingerprint_window(24000 / 1001 / 25) == "intro@0.959041"
    assert fpmod.fingerprint_window(25 / (24000 / 1001)) == "intro@1.042708"


@pytest.mark.parametrize(
    ("stdout", "returncode", "found"),
    [
        (" D  webm_chunk  WebM Chunk Muxer\n E  chromaprint     Chromaprint\n", 0, True),
        (" E  mp4             MP4 (MPEG-4 Part 14)\n", 0, False),
        (" E  chromaprint     Chromaprint\n", 1, False),
    ],
)
def test_has_chromaprint_reads_the_muxer_list(stdout, returncode, found):
    with patch.object(fpmod.subprocess, "run", return_value=MagicMock(stdout=stdout, returncode=returncode)) as run:
        assert fpmod.has_chromaprint("/x/ffmpeg") is found
    assert run.call_args.args[0] == ["/x/ffmpeg", "-hide_banner", "-muxers"]


@pytest.mark.parametrize(
    "stdout",
    [" E  mp4             MP4 (MPEG-4 Part 14)\n", " E  chromaprint     Chromaprint\n"],
    ids=["without", "with"],
)
def test_an_ffmpeg_that_listed_its_muxers_is_never_asked_again(monkeypatch, stdout):
    clock = _Clock()
    monkeypatch.setattr(fpmod, "_monotonic", clock)
    with patch.object(fpmod.subprocess, "run", return_value=MagicMock(stdout=stdout, returncode=0)) as run:
        first = fpmod.has_chromaprint("/x/ffmpeg")
        clock.now += 30 * 86_400
        assert fpmod.has_chromaprint("/x/ffmpeg") is first
    assert run.call_count == 1


@pytest.mark.parametrize(
    "failure",
    [
        {"side_effect": subprocess.TimeoutExpired("ffmpeg", 20)},
        {"side_effect": OSError("Text file busy")},
        {"return_value": MagicMock(stdout="", returncode=1)},
    ],
    ids=["timed-out", "couldnt-start", "exited-with-an-error"],
)
def test_an_ffmpeg_that_didnt_list_its_muxers_is_asked_again_after_10_minutes(monkeypatch, failure):
    clock = _Clock()
    monkeypatch.setattr(fpmod, "_monotonic", clock)
    listed = MagicMock(stdout=" E  chromaprint     Chromaprint\n", returncode=0)
    with patch.object(fpmod.subprocess, "run", **failure) as run:
        assert fpmod.has_chromaprint("/x/ffmpeg") is False
        clock.now += fpmod.CHROMAPRINT_RETRY_S - 1
        assert fpmod.has_chromaprint("/x/ffmpeg") is False
        assert run.call_count == 1
    with patch.object(fpmod.subprocess, "run", return_value=listed) as run:
        clock.now += 1
        assert fpmod.has_chromaprint("/x/ffmpeg") is True
    assert run.call_count == 1


def test_callers_checking_one_ffmpeg_at_once_share_one_check(monkeypatch):
    second_asking, answers = threading.Event(), []
    listed = MagicMock(stdout=" E  chromaprint     Chromaprint\n", returncode=0)

    def muxers(*args, **kwargs):
        second_asking.wait(5)
        time.sleep(0.1)  # the second caller reaches the check while this one still runs
        return listed

    def ask():
        answers.append(fpmod.has_chromaprint("/x/ffmpeg"))

    with patch.object(fpmod.subprocess, "run", side_effect=muxers) as run:
        first = threading.Thread(target=ask)
        first.start()
        second = threading.Thread(target=lambda: (second_asking.set(), ask()))
        second.start()
        first.join(10)
        second.join(10)
    assert answers == [True, True]
    assert run.call_count == 1


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ({"conf": "absent", "jf": "available"}, "available"),
        ({"conf": "unknown", "jf": "available"}, "available"),
        ({"conf": "unknown", "jf": "absent"}, "unknown"),
        ({"conf": "absent", "jf": "absent"}, "absent"),
        ({}, "absent"),
    ],
    ids=["one-has-it", "one-unknown-one-has-it", "one-unknown", "none-has-it", "no-ffmpeg"],
)
def test_the_chromaprint_state_is_available_then_unknown_then_absent(tmp_path, states, expected):
    binaries = {}
    for name in states:
        binary = tmp_path / name
        binary.write_text("")
        binary.chmod(0o755)
        binaries[str(binary)] = fpmod.ChromaprintState(states[name])
    missing = str(tmp_path / "missing")
    with (
        patch.object(fpmod, "JELLYFIN_FFMPEG", str(tmp_path / "jf") if "jf" in states else missing),
        patch.object(fpmod.shutil, "which", return_value=None),
        patch.object(fpmod, "muxer_state", side_effect=lambda ffmpeg: binaries[ffmpeg]),
    ):
        configured = str(tmp_path / "conf") if "conf" in states else missing
        assert fpmod.chromaprint_state(configured) is fpmod.ChromaprintState(expected)
        found, reason = fpmod.chromaprint_status(configured)
    assert (found is not None) is (expected == "available")
    assert ("checked again in 10 minutes" in reason) is (expected == "unknown")


def test_chromaprint_ffmpeg_prefers_configured_then_jellyfin_then_path(tmp_path):
    configured, jellyfin, on_path = (tmp_path / n for n in ("conf", "jf", "path"))
    for p in (configured, jellyfin, on_path):
        p.write_text("")
        p.chmod(0o755)
    with (
        patch.object(fpmod, "JELLYFIN_FFMPEG", str(jellyfin)),
        patch.object(fpmod.shutil, "which", return_value=str(on_path)),
        patch.object(fpmod, "has_chromaprint", side_effect=lambda f: f != str(configured)),
    ):
        assert fpmod.chromaprint_ffmpeg(str(configured)) == str(jellyfin)
    with (
        patch.object(fpmod, "JELLYFIN_FFMPEG", str(tmp_path / "missing")),
        patch.object(fpmod.shutil, "which", return_value=str(on_path)),
        patch.object(fpmod, "has_chromaprint", return_value=False),
    ):
        assert fpmod.chromaprint_ffmpeg(None) is None
        found, reason = fpmod.chromaprint_status(None)
    assert found is None and "chromaprint" in reason


def test_compute_returns_the_raw_points():
    raw = np.array([1, 2, 0xFFFFFFFF], dtype="<u4").tobytes() + b"\x07"  # a torn last point is dropped
    with patch.object(fpmod.subprocess, "Popen", return_value=_proc(stdout=raw)) as popen:
        points = fpmod.compute_fingerprint("/m/a.mkv", 1_321_472, ffmpeg="ffmpeg")
    assert points.tolist() == [1, 2, 0xFFFFFFFF] and points.dtype == np.dtype("<u4")
    assert popen.call_args.args[0] == fpmod.fingerprint_command("ffmpeg", "/m/a.mkv", fpmod.window_s(1_321_472))


def test_compute_runs_in_a_session_of_its_own():
    # Its own session, so a pause stops (and a cancel kills) ffmpeg's whole group and never the app's.
    with patch.object(fpmod.subprocess, "Popen", return_value=_proc(stdout=b"")) as popen:
        fpmod.compute_fingerprint("/m/a.mkv", 1_321_472, ffmpeg="ffmpeg")
    assert popen.call_args.args[0] == fpmod.fingerprint_command("ffmpeg", "/m/a.mkv", fpmod.window_s(1_321_472))
    assert popen.call_args.kwargs["start_new_session"] is True


def test_a_failing_pause_or_cancel_check_still_stops_ffmpeg():
    # Both are asked while ffmpeg runs; whatever they raise must not leave it running with its pipes held.
    proc = _proc(hang=True)

    def broken():
        raise RuntimeError("settings unreadable")

    with patch.object(fpmod.subprocess, "Popen", return_value=proc), pytest.raises(RuntimeError, match="unreadable"):
        fpmod.compute_fingerprint("/m/a.mkv", 60_000, ffmpeg="ffmpeg", cancel_check=broken)
    proc.kill.assert_called_once()


def test_a_cancel_during_a_pause_before_the_start_starts_no_ffmpeg():
    cancelled = threading.Event()
    paused = threading.Event()
    paused.set()

    def pause_check():
        cancelled.set()  # cancelled while everything is paused
        return paused.is_set()

    with patch.object(fpmod.subprocess, "Popen") as popen, pytest.raises(fpmod.FingerprintError, match="cancelled"):
        fpmod.compute_fingerprint("/m/a.mkv", 60_000, ffmpeg="ffmpeg", pause_check=pause_check,
                                  cancel_check=cancelled.is_set)  # fmt: skip
    popen.assert_not_called()


def test_a_mount_that_stalled_during_a_pause_before_the_start_starts_no_ffmpeg(monkeypatch):
    paused, stalled = [True], [0]

    def pause_check():
        return paused[0]

    def sleep(_seconds):
        stalled[0] = fpmod.STALLED_LIMIT  # other files' reads got stuck on the mount while everything was paused
        paused[0] = False

    from media_preview_generator.markers import freeze

    monkeypatch.setattr(freeze, "time", SimpleNamespace(monotonic=time.monotonic, sleep=sleep))
    with (
        patch.object(fpmod, "stalled_ffmpegs", lambda: stalled[0]),
        patch.object(fpmod.subprocess, "Popen") as popen,
        pytest.raises(fpmod.FingerprintStalledError),
    ):
        fpmod.compute_fingerprint("/m/a.mkv", 60_000, ffmpeg="ffmpeg", pause_check=pause_check)
    popen.assert_not_called()


def test_compute_runs_the_retimed_command():
    with patch.object(fpmod.subprocess, "Popen", return_value=_proc(stdout=b"")) as popen:
        fpmod.compute_fingerprint("/m/a.mkv", 1_321_472, ffmpeg="ffmpeg", retime=0.95)
    assert popen.call_args.args[0] == fpmod.fingerprint_command(
        "ffmpeg", "/m/a.mkv", fpmod.window_s(1_321_472), retime=0.95
    )


def test_file_without_audio_gives_an_empty_fingerprint():
    err = b"Output file #0 does not contain any stream\n"
    with patch.object(fpmod.subprocess, "Popen", return_value=_proc(stderr=err, returncode=1)):
        assert fpmod.compute_fingerprint("/m/a.mkv", 60_000, ffmpeg="ffmpeg").size == 0


def test_other_ffmpeg_errors_raise():
    with patch.object(fpmod.subprocess, "Popen", return_value=_proc(stderr=b"Invalid data found", returncode=1)):
        with pytest.raises(fpmod.FingerprintError, match="exited 1"):
            fpmod.compute_fingerprint("/m/a.mkv", 60_000, ffmpeg="ffmpeg")


def test_cancel_kills_ffmpeg_and_collects_it_within_the_kill_wait():
    proc = _proc(hang=True)
    with patch.object(fpmod.subprocess, "Popen", return_value=proc):
        with pytest.raises(fpmod.FingerprintError, match="cancelled"):
            fpmod.compute_fingerprint("/m/a.mkv", 60_000, ffmpeg="ffmpeg", cancel_check=lambda: True)
    proc.kill.assert_called_once()
    assert proc.communicate.call_args_list[-1].kwargs == {"timeout": fpmod.KILL_WAIT_S}
    assert fpmod.stalled_ffmpegs() == 0  # collected at once: never counted as stuck


@pytest.mark.parametrize("stop", ["timeout", "cancel"])
def test_an_ffmpeg_whose_output_outlives_the_kill_never_holds_the_worker(tmp_path, monkeypatch, stop):
    # A read stalled on a hard network mount leaves ffmpeg unkillable and its pipes open for as long as the stall
    # lasts. Here a process in a session of its own keeps the pipes open after ffmpeg is killed, the same shape.
    monkeypatch.setattr(fpmod, "KILL_WAIT_S", 0.5)
    pid_file = tmp_path / "holder.pid"
    script = tmp_path / "ffmpeg"
    script.write_text(f'#!/bin/sh\nsetsid sleep 60 &\necho $! > "{pid_file}"\nexec sleep 60\n')
    script.chmod(0o755)
    started_procs: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def popen(*args, **kwargs):
        started_procs.append(real_popen(*args, **kwargs))
        return started_procs[-1]

    monkeypatch.setattr(fpmod.subprocess, "Popen", popen)
    started = time.monotonic()
    try:
        with pytest.raises(fpmod.FingerprintError, match="cancelled" if stop == "cancel" else "timed out"):
            fpmod.compute_fingerprint(
                str(tmp_path / "a.mkv"),
                60_000,
                ffmpeg=str(script),
                cancel_check=(lambda: time.monotonic() - started > 0.5) if stop == "cancel" else None,
                timeout_s=0.5,
            )
        elapsed = time.monotonic() - started
        (proc,) = started_procs
        assert proc.returncode is None  # handed on, not collected on the worker thread
    finally:
        for _ in range(50):
            if pid_file.exists() and pid_file.read_text().strip():
                break
            time.sleep(0.1)
        os.kill(int(pid_file.read_text()), 9)
    assert elapsed < 0.5 + 0.5 + 3.0  # bounded, not the 60 s the pipe is held
    # Once the pipe is let go, the reaper collects the killed ffmpeg: no zombie is left behind.
    for reaper in [t for t in threading.enumerate() if t.name == "fingerprint-reaper"]:
        reaper.join(10)
    assert proc.returncode == -9
    assert fpmod.stalled_ffmpegs() == 0  # counted down once collected


def _fake_ffmpeg(tmp_path, *, steps: int = 20, step_s: float = 0.05) -> pathlib.Path:
    """An ffmpeg stand-in that counts its steps into ``progress``, then writes two fingerprint points."""
    pid_file, progress = tmp_path / "ffmpeg.pid", tmp_path / "progress"
    script = tmp_path / "ffmpeg"
    script.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(f"""
        import os, sys, time
        open({str(pid_file)!r}, "w").write(str(os.getpid()))
        for n in range({steps}):
            open({str(progress)!r} + ".tmp", "w").write(str(n + 1))
            os.replace({str(progress)!r} + ".tmp", {str(progress)!r})
            time.sleep({step_s})
        sys.stdout.buffer.write(bytes([1, 0, 0, 0, 2, 0, 0, 0]))
    """)
    )
    script.chmod(0o755)
    return script


def _state(pid: int) -> str:
    return pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split(" ", 1)[0]


def _progress(path: pathlib.Path) -> int:
    try:
        return int(path.read_text() or 0)
    except (FileNotFoundError, ValueError):
        return 0


def _wait_for(condition, *, within_s: float = 5.0) -> bool:
    deadline = time.monotonic() + within_s
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.02)
    return condition()


class TestPause:
    """Pause all, quiet hours and a schedule's stop time stop a running fingerprint where it is, as previews' FFmpeg."""

    @pytest.fixture
    def group_signals(self, monkeypatch):
        from media_preview_generator.markers import freeze

        monkeypatch.setattr(fpmod, "_POLL_S", 0.05)
        sent: list[tuple[int, int]] = []
        real = os.killpg

        def killpg(pgid, sig):
            sent.append((pgid, sig))
            real(pgid, sig)

        monkeypatch.setattr(freeze.os, "killpg", killpg)
        return sent

    @staticmethod
    def _in_background(**kwargs):
        out: dict = {}

        def run():
            try:
                out["points"] = fpmod.compute_fingerprint("/m/a.mkv", 60_000, **kwargs).tolist()
            except BaseException as exc:  # noqa: BLE001 - collected for the assertions
                out["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, out

    def test_a_pause_stops_the_group_and_the_resume_goes_on_with_the_deadline_moved_out(self, tmp_path, group_signals):
        ffmpeg = _fake_ffmpeg(tmp_path)  # about 1 s of work
        progress = tmp_path / "progress"
        paused = threading.Event()
        thread, out = self._in_background(ffmpeg=str(ffmpeg), pause_check=paused.is_set, timeout_s=2.5)
        try:
            assert _wait_for(lambda: _progress(progress) >= 3)
            paused.set()
            pid = int((tmp_path / "ffmpeg.pid").read_text())
            assert _wait_for(lambda: _state(pid) == "T")
            frozen_at = _progress(progress)
            time.sleep(3.0)  # past the 2.5 s limit
            assert _state(pid) == "T" and _progress(progress) == frozen_at
            assert thread.is_alive()
        finally:
            paused.clear()
            thread.join(10)
        assert out == {"points": [1, 2]}
        assert group_signals == [(pid, signal.SIGSTOP), (pid, signal.SIGCONT)]

    def test_a_cancel_while_paused_kills_the_frozen_ffmpeg(self, tmp_path, group_signals):
        ffmpeg = _fake_ffmpeg(tmp_path, steps=200)
        paused, cancelled = threading.Event(), threading.Event()
        thread, out = self._in_background(ffmpeg=str(ffmpeg), pause_check=paused.is_set,
                                          cancel_check=cancelled.is_set)  # fmt: skip
        assert _wait_for(lambda: _progress(tmp_path / "progress") >= 2)
        paused.set()
        pid = int((tmp_path / "ffmpeg.pid").read_text())
        assert _wait_for(lambda: _state(pid) == "T")
        started = time.monotonic()
        cancelled.set()
        thread.join(10)
        assert isinstance(out.get("error"), fpmod.FingerprintError) and "cancelled" in str(out["error"])
        assert time.monotonic() - started < 3
        assert not pathlib.Path(f"/proc/{pid}").exists() or _state(pid) == "Z"
        assert group_signals[:2] == [(pid, signal.SIGSTOP), (pid, signal.SIGCONT)]

    def test_no_ffmpeg_starts_while_paused(self, tmp_path, group_signals):
        ffmpeg = _fake_ffmpeg(tmp_path, steps=1)
        paused = threading.Event()
        paused.set()
        thread, out = self._in_background(ffmpeg=str(ffmpeg), pause_check=paused.is_set, timeout_s=1.0)
        time.sleep(1.5)  # past the time limit: it counts from the start of ffmpeg
        assert not (tmp_path / "ffmpeg.pid").exists() and thread.is_alive()
        paused.clear()
        thread.join(10)
        assert out == {"points": [1, 2]}
        assert group_signals == []


def _record(store, tmp_path, name="S01E01.mkv"):
    path = tmp_path / name
    path.write_bytes(b"x" * 10)
    return _record_at(store, path)


def _record_at(store, path):
    st = path.stat()
    return store.upsert_file(
        FileIdentity(str(path), st.st_size, st.st_mtime_ns),
        duration_ms=300_000,
        season_key=str(path.parent),
        is_movie=False,
    )


def test_ensure_computes_once_then_reads_the_cache(store, tmp_path):
    rec = _record(store, tmp_path)
    with patch.object(fpmod, "compute_fingerprint", return_value=np.array([5, 6], dtype="<u4")) as compute:
        first = fpmod.ensure_fingerprint(store, rec, ffmpeg="ffmpeg")
        second = fpmod.ensure_fingerprint(store, rec, ffmpeg="ffmpeg")
    assert first.tolist() == second.tolist() == [5, 6]
    compute.assert_called_once_with(
        rec.canonical_path,
        300_000,
        ffmpeg="ffmpeg",
        cancel_check=None,
        pause_check=None,
        retime=None,
    )
    stored = store.get_fingerprint(rec.id, "intro")
    assert (stored.start_s, stored.length_s, stored.algorithm) == (0.0, 105.0, 1)


def test_a_retimed_fingerprint_is_computed_and_cached_beside_the_files_own(store, tmp_path):
    rec = _record(store, tmp_path)
    retime = 24000 / 1001 / 25
    with patch.object(fpmod, "compute_fingerprint", return_value=np.array([7, 8], dtype="<u4")) as compute:
        assert fpmod.ensure_fingerprint(store, rec, ffmpeg="ffmpeg", retime=retime).tolist() == [7, 8]
        assert fpmod.ensure_fingerprint(store, rec, ffmpeg="ffmpeg", retime=retime).tolist() == [7, 8]
    compute.assert_called_once_with(
        rec.canonical_path,
        300_000,
        ffmpeg="ffmpeg",
        cancel_check=None,
        pause_check=None,
        retime=retime,
    )
    stored = store.get_fingerprint(rec.id, "intro@0.959041")
    assert (stored.start_s, stored.length_s, stored.algorithm, stored.points) == (
        0.0,
        105.0,
        1,
        bytes(np.array([7, 8], dtype="<u4")),
    )
    assert store.get_fingerprint(rec.id, "intro") is None
    assert fpmod.has_cached_fingerprint(store, rec) is False
    assert fpmod.has_cached_fingerprint(store, rec, retime=retime) is True
    assert fpmod.cached_fingerprint(store, rec, retime=retime) == stored
    assert fpmod.cached_fingerprint(store, rec, retime=25 / (24000 / 1001)) is None


def test_ensure_stores_nothing_for_a_file_replaced_meanwhile(store, tmp_path):
    rec = _record(store, tmp_path)
    store.upsert_file(FileIdentity(rec.canonical_path, 999, 1), duration_ms=300_000, season_key="x", is_movie=False)
    with patch.object(fpmod, "compute_fingerprint", return_value=np.array([5], dtype="<u4")):
        assert fpmod.ensure_fingerprint(store, rec, ffmpeg="ffmpeg") is None
    assert store.get_fingerprint(rec.id, "intro") is None


def test_skip_is_asked_after_the_lock_and_a_cache_miss(store, tmp_path):
    rec = _record(store, tmp_path)
    with patch.object(fpmod, "compute_fingerprint", return_value=np.array([5], dtype="<u4")) as compute:
        with pytest.raises(fpmod.FingerprintSkippedError):
            fpmod.ensure_fingerprint(store, rec, ffmpeg="ffmpeg", skip=lambda: True)
        compute.assert_not_called()
        fpmod.ensure_fingerprint(store, rec, ffmpeg="ffmpeg", skip=lambda: False)
        assert fpmod.ensure_fingerprint(store, rec, ffmpeg="ffmpeg", skip=lambda: True).tolist() == [5]  # cached
    compute.assert_called_once_with(
        rec.canonical_path,
        rec.duration_ms,
        ffmpeg="ffmpeg",
        cancel_check=None,
        pause_check=None,
        retime=None,
    )


@pytest.mark.parametrize(("cancelled", "recorded"), [(False, True), (True, False)], ids=["failed", "cancelled"])
def test_a_failure_is_reported_while_the_files_lock_is_still_held(store, tmp_path, cancelled, recorded):
    rec = _record(store, tmp_path)
    held_at_failure = []

    def on_failure():
        entry = fpmod._FILE_LOCKS._locks.get(rec.id)
        held_at_failure.append(entry is not None and entry[0].locked())

    with patch.object(fpmod, "compute_fingerprint", side_effect=fpmod.FingerprintError("ffmpeg exited 1")):
        with pytest.raises(fpmod.FingerprintError):
            fpmod.ensure_fingerprint(store, rec, ffmpeg="ffmpeg", cancel_check=lambda: cancelled, on_failure=on_failure)
    assert held_at_failure == ([True] if recorded else [])


def test_eight_workers_fingerprint_at_once_with_no_app_wide_limit(store, tmp_path):
    # The worker pool is the cap, as it is for previews: a worker never holds its slot waiting for an app-wide
    # fingerprint limit. All eight must be inside ffmpeg together for the barrier to open; one held back would break it.
    recs = [_record(store, tmp_path, f"S01E0{i}.mkv") for i in range(1, 9)]
    together = threading.Barrier(len(recs), timeout=5)
    results: list[object] = []

    def compute(*_a, **_kw):
        together.wait()
        return np.array([1], dtype="<u4")

    def work(rec):
        try:
            results.append(fpmod.ensure_fingerprint(store, rec, ffmpeg="f").tolist())
        except BaseException as exc:  # noqa: BLE001 - collected for the assertion
            results.append(exc)

    with patch.object(fpmod, "compute_fingerprint", side_effect=compute):
        threads = [threading.Thread(target=work, args=(r,)) for r in recs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
    assert results == [[1]] * len(recs)
    assert not hasattr(fpmod, "_PARALLEL") and not hasattr(fpmod, "MAX_PARALLEL")


def test_the_pause_reaches_the_ffmpeg_run(store, tmp_path):
    rec = _record(store, tmp_path)

    def paused():
        return False

    with patch.object(fpmod, "compute_fingerprint", return_value=np.array([5], dtype="<u4")) as compute:
        fpmod.ensure_fingerprint(store, rec, ffmpeg="ffmpeg", pause_check=paused)
    assert compute.call_args.kwargs["pause_check"] is paused


class _StuckProc:
    """An ffmpeg stuck in a read on a stalled mount: killing it changes nothing until ``released``, then it exits."""

    def __init__(self):
        self.released = threading.Event()
        self.returncode = None

    def kill(self):
        pass

    def communicate(self, timeout=None):
        if not self.released.wait(timeout):
            raise subprocess.TimeoutExpired("ffmpeg", timeout)
        self.returncode = -9
        return b"", b""


class TestAFileAnotherRunHolds:
    """Another job's run of the same file holds its fingerprint lock; one frozen by its schedule's stop time holds it
    until the schedule's next start. A waiter never waits longer than a running fingerprint takes, and a cancel ends
    its wait at once."""

    @pytest.fixture
    def held(self, store, tmp_path, monkeypatch):
        monkeypatch.setattr(fpmod, "LOCK_WAIT_S", 0.3)
        rec = _record(store, tmp_path)
        holding, release = threading.Event(), threading.Event()

        def hold():
            with fpmod._FILE_LOCKS.hold(rec.id):
                holding.set()
                release.wait(10)

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        assert holding.wait(5)
        yield rec, release
        release.set()
        holder.join(5)

    @pytest.mark.parametrize("sibling", [True, False], ids=["sibling", "own-file"])
    def test_a_waiter_gives_up_after_the_longest_a_running_fingerprint_takes(self, store, held, sibling):
        rec, _release = held
        failures: list[int] = []
        started = time.monotonic()
        with (
            patch.object(fpmod, "compute_fingerprint") as compute,
            pytest.raises(fpmod.FingerprintBusyError, match="another job"),
        ):
            fpmod.ensure_fingerprint(store, rec, ffmpeg="ffmpeg", skip=(lambda: False) if sibling else None,
                                     on_failure=lambda: failures.append(1))  # fmt: skip
        assert 0.3 <= time.monotonic() - started < 2
        compute.assert_not_called()
        assert failures == []  # not the file's fault: nothing is recorded against it

    def test_a_cancel_ends_the_wait_at_once(self, store, held, monkeypatch):
        monkeypatch.setattr(fpmod, "LOCK_WAIT_S", 30.0)
        rec, _release = held
        cancelled = threading.Event()
        threading.Timer(0.2, cancelled.set).start()
        started = time.monotonic()
        with (
            patch.object(fpmod, "compute_fingerprint") as compute,
            pytest.raises(fpmod.FingerprintError, match="cancelled"),
        ):
            fpmod.ensure_fingerprint(store, rec, ffmpeg="ffmpeg", cancel_check=cancelled.is_set)
        assert time.monotonic() - started < 1.5
        compute.assert_not_called()

    def test_time_the_waiter_itself_is_paused_doesnt_count(self, store, held):
        # Pause all freezes both runs: the waiter waits it out and takes the file once it is free.
        rec, release = held
        paused = threading.Event()
        paused.set()
        threading.Timer(0.8, release.set).start()
        threading.Timer(1.0, paused.clear).start()
        with patch.object(fpmod, "compute_fingerprint", return_value=np.array([4], dtype="<u4")) as compute:
            assert fpmod.ensure_fingerprint(store, rec, ffmpeg="ffmpeg", skip=lambda: False,
                                            pause_check=paused.is_set).tolist() == [4]  # fmt: skip
        compute.assert_called_once()


class TestStalledFfmpegs:
    """A killed ffmpeg that still holds its output gives back the file's lock, but counts as stalled until it is
    collected: at ``STALLED_LIMIT`` stalled, no new ffmpeg starts on what is most likely the same stalled mount."""

    @pytest.fixture
    def stuck(self, monkeypatch):
        monkeypatch.setattr(fpmod, "KILL_WAIT_S", 0.3)
        monkeypatch.setattr(fpmod, "_POLL_S", 0.05)
        procs: list[_StuckProc] = []

        def popen(*args, **kwargs):
            procs.append(_StuckProc())
            return procs[-1]

        monkeypatch.setattr(fpmod.subprocess, "Popen", popen)
        yield procs
        for proc in procs:
            proc.released.set()
        for reaper in [t for t in threading.enumerate() if t.name == "fingerprint-reaper"]:
            reaper.join(5)
        assert fpmod.stalled_ffmpegs() == 0

    def _stall(self, store, rec):
        with pytest.raises(fpmod.FingerprintError, match="cancelled"):
            fpmod.ensure_fingerprint(store, rec, ffmpeg="ffmpeg", cancel_check=lambda: True)

    def test_two_stalled_ffmpegs_keep_a_third_from_starting(self, store, tmp_path, stuck):
        first, second, third = (_record(store, tmp_path, f"S01E0{i}.mkv") for i in (1, 2, 3))
        self._stall(store, first)
        self._stall(store, second)
        assert fpmod.stalled_ffmpegs() == 2
        failures: list[str] = []
        with pytest.raises(fpmod.FingerprintStalledError, match="2 earlier fingerprint ffmpegs are still stuck"):
            fpmod.ensure_fingerprint(store, third, ffmpeg="ffmpeg", on_failure=lambda: failures.append("x"))
        assert len(stuck) == 2  # no ffmpeg started
        assert failures == []  # the mount's fault, not the file's: nothing counts against it

    def test_the_count_comes_down_as_stalled_ffmpegs_finally_exit(self, store, tmp_path, stuck, monkeypatch):
        first, second, third = (_record(store, tmp_path, f"S01E0{i}.mkv") for i in (1, 2, 3))
        self._stall(store, first)
        self._stall(store, second)
        stuck[0].released.set()  # the stall ends for one of them
        deadline = time.monotonic() + 5
        while fpmod.stalled_ffmpegs() != 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert fpmod.stalled_ffmpegs() == 1
        raw = np.array([7, 8], dtype="<u4").tobytes()
        with patch.object(fpmod.subprocess, "Popen", return_value=_proc(stdout=raw)) as popen:
            assert fpmod.ensure_fingerprint(store, third, ffmpeg="ffmpeg").tolist() == [7, 8]
        assert popen.call_args.args[0] == fpmod.fingerprint_command("ffmpeg", third.canonical_path, 105.0)

    def test_other_files_stalled_ffmpegs_refuse_a_caller_at_once(self, store, tmp_path, stuck):
        rec = _record(store, tmp_path, "S01E01.mkv")
        for name in ("S02E01.mkv", "S02E02.mkv"):  # other files' ffmpegs, killed and handed on while still stuck
            stuck.append(_StuckProc())
            fpmod._stop(stuck[-1], name)
        started = time.monotonic()
        with pytest.raises(fpmod.FingerprintStalledError):
            fpmod.ensure_fingerprint(store, rec, ffmpeg="ffmpeg")
        assert time.monotonic() - started < 0.5
        assert len(stuck) == 2  # no ffmpeg started


@pytest.mark.parametrize("made", [{"algorithm": 2}, {"length_s": 60.0}], ids=["other-algorithm", "other-window"])
def test_a_fingerprint_made_another_way_is_computed_again(store, tmp_path, made):
    rec = _record(store, tmp_path)
    old = {"start_s": 0.0, "length_s": fpmod.window_s(rec.duration_ms), "algorithm": fpmod.ALGORITHM, **made}
    store.set_fingerprint(
        rec.id, size=rec.size, mtime_ns=rec.mtime_ns, window="intro", points=b"\x01\x00\x00\x00", **old
    )
    other = _record(store, tmp_path, "S01E02.mkv")
    store.set_fingerprint(other.id, size=other.size, mtime_ns=other.mtime_ns, window="intro", start_s=0.0,
                          length_s=fpmod.window_s(other.duration_ms), algorithm=fpmod.ALGORITHM, points=b"")  # fmt: skip
    store.set_season_pair(rec.id, other.id, 4, [], identity_a=(rec.size, rec.mtime_ns),
                          identity_b=(other.size, other.mtime_ns))  # fmt: skip
    assert fpmod.has_cached_fingerprint(store, rec) is False
    with patch.object(fpmod, "compute_fingerprint", return_value=np.array([5, 6], dtype="<u4")) as compute:
        assert fpmod.ensure_fingerprint(store, rec, ffmpeg="ffmpeg").tolist() == [5, 6]
    compute.assert_called_once_with(
        rec.canonical_path,
        rec.duration_ms,
        ffmpeg="ffmpeg",
        cancel_check=None,
        pause_check=None,
        retime=None,
    )
    stored = store.get_fingerprint(rec.id, "intro")
    assert (stored.algorithm, stored.length_s) == (fpmod.ALGORITHM, fpmod.window_s(rec.duration_ms))
    assert fpmod.has_cached_fingerprint(store, rec) is True
    assert store.get_season_pair(rec.id, other.id, 4) is None  # matched with the old points


class TestCacheSweep:
    @staticmethod
    def _fingerprinted(store, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 10)
        rec = _record_at(store, path)
        store.set_fingerprint(rec.id, size=rec.size, mtime_ns=rec.mtime_ns, window="intro", start_s=0.0,
                              length_s=fpmod.window_s(rec.duration_ms), algorithm=fpmod.ALGORITHM, points=b"")  # fmt: skip
        return rec

    def test_a_renamed_files_fingerprint_pairs_and_failures_go_and_its_row_stays(self, store, tmp_path):
        season = tmp_path / "Show" / "Season 01"
        old = self._fingerprinted(store, season / "Show - S01E01 - WEB.mkv")
        live = self._fingerprinted(store, season / "Show - S01E02.mkv")
        store.set_season_pair(old.id, live.id, 4, [], identity_a=(old.size, old.mtime_ns),
                              identity_b=(live.size, live.mtime_ns))  # fmt: skip
        identity = FileIdentity(old.canonical_path, old.size, old.mtime_ns)
        now = datetime(2026, 9, 13, tzinfo=UTC)
        store.record_member_fingerprint_failure(identity, now, forget_before=now)
        store.record_member_probe_failure(identity, now, forget_before=now)
        os.rename(old.canonical_path, season / "Show - S01E01 - Bluray.mkv")  # a quality upgrade

        assert fpmod.sweep_fingerprint_cache(store) == 1

        assert store.get_fingerprint(old.id, "intro") is None
        assert store.get_season_pair(old.id, live.id, 4) is None
        assert store.member_fingerprint_failed_at(identity) is None and store.member_probe_failed_at(identity) is None
        assert store.get_file(old.canonical_path) == old
        assert store.get_fingerprint(live.id, "intro") is not None

    def test_a_file_whose_folder_is_missing_keeps_its_fingerprint(self, store, tmp_path):
        rec = self._fingerprinted(store, tmp_path / "Show" / "Season 01" / "Show - S01E01.mkv")
        shutil.rmtree(tmp_path / "Show")  # an unmounted library looks the same

        assert fpmod.sweep_fingerprint_cache(store) == 0
        assert store.get_fingerprint(rec.id, "intro") is not None

    def test_a_file_that_cant_be_read_keeps_its_fingerprint(self, store, tmp_path):
        rec = self._fingerprinted(store, tmp_path / "Show" / "Season 01" / "Show - S01E01.mkv")
        real_stat = os.stat

        def stat(path, *args, **kwargs):  # the folder reads fine, the file doesn't (a stalled network mount)
            if str(path) == rec.canonical_path:
                raise OSError(errno.ESTALE, "Stale file handle")
            return real_stat(path, *args, **kwargs)

        with patch.object(fpmod.os, "stat", side_effect=stat):
            assert fpmod.sweep_fingerprint_cache(store) == 0
        assert store.get_fingerprint(rec.id, "intro") is not None

    def test_each_sweep_checks_at_most_2000_files_and_the_next_one_goes_on_from_there(self, store, tmp_path):
        folder = tmp_path / "Show" / "Season 01"
        folder.mkdir(parents=True)
        recs = []
        for n in range(2_500):
            rec = store.upsert_file(FileIdentity(str(folder / f"gone {n:04d}.mkv"), 1, 1), duration_ms=300_000,
                                    season_key=str(folder), is_movie=False)  # fmt: skip
            store.set_fingerprint(rec.id, size=1, mtime_ns=1, window="intro", start_s=0.0, length_s=105.0,
                                  algorithm=1, points=b"")  # fmt: skip
            recs.append(rec)

        assert fpmod.sweep_fingerprint_cache(store) == 2_000
        assert [store.has_fingerprint(r.id, "intro") for r in (recs[1_999], recs[2_000])] == [False, True]
        assert fpmod.sweep_fingerprint_cache(store) == 500
        assert not any(store.has_fingerprint(r.id, "intro") for r in recs)

    def test_a_sweep_out_of_time_stops_and_the_next_goes_on_from_the_first_file_it_left(
        self, store, tmp_path, monkeypatch
    ):
        recs = [self._fingerprinted(store, tmp_path / "Show" / "Season 01" / f"S01E{n:02d}.mkv") for n in range(1, 7)]
        clock = _Clock()
        monkeypatch.setattr(fpmod, "_monotonic", clock)
        real_stat, checked = os.stat, []

        def slow_stat(path, *args, **kwargs):  # each file takes 25 s to read: two fit in the 60 s budget, not three
            if str(path) in {r.canonical_path for r in recs}:
                checked.append(str(path))
                clock.now += 25
            return real_stat(path, *args, **kwargs)

        with patch.object(fpmod.os, "stat", side_effect=slow_stat):
            fpmod.sweep_fingerprint_cache(store)
            assert checked == [r.canonical_path for r in recs[:3]]
            checked.clear()
            fpmod.sweep_fingerprint_cache(store)
        assert checked == [r.canonical_path for r in recs[3:6]]


class TestBackgroundSweep:
    @pytest.fixture(autouse=True)
    def fresh(self, monkeypatch):
        self.clock = _Clock()
        monkeypatch.setattr(fpmod, "_monotonic", self.clock)
        monkeypatch.setattr(fpmod, "_sweep_started_at", None)
        monkeypatch.setattr(fpmod, "_stuck_warned_at", None)
        yield
        assert fpmod._SWEEP_LOCK.acquire(timeout=5)
        fpmod._SWEEP_LOCK.release()

    @pytest.fixture
    def blocked_sweep(self):
        """A sweep that stays running until the test ends (or 10 s pass)."""
        entered, release = threading.Event(), threading.Event()

        def sweep(store):
            entered.set()
            release.wait(10)
            return 0

        with patch.object(fpmod, "sweep_fingerprint_cache", side_effect=sweep) as swept:
            yield entered, swept
            release.set()

    def test_one_sweep_at_a_time_and_a_stuck_one_is_warned_about(self, loguru_caplog, blocked_sweep):
        entered, swept = blocked_sweep
        assert fpmod.start_fingerprint_sweep(MagicMock()) is True
        assert entered.wait(5)
        self.clock.now += fpmod.SWEEP_MIN_GAP_S  # the gap is over, but the sweep still runs
        assert fpmod.start_fingerprint_sweep(MagicMock()) is False
        assert "still waiting on the file system" in loguru_caplog.text  # running for more than 10 minutes
        assert swept.call_count == 1

    def test_a_stuck_sweep_is_warned_about_at_most_once_every_10_minutes(self, loguru_caplog, blocked_sweep):
        entered, _ = blocked_sweep
        assert fpmod.start_fingerprint_sweep(MagicMock()) is True
        assert entered.wait(5)
        warned = []
        for minutes in (11, 12, 20, 21, 22):  # jobs completing while the sweep stays stuck
            self.clock.now = 1_000.0 + minutes * 60
            assert fpmod.start_fingerprint_sweep(MagicMock()) is False
            warned.append(loguru_caplog.text.count("still waiting on the file system"))
        assert warned == [1, 1, 1, 2, 2]

    def test_no_warning_for_a_sweep_running_less_than_10_minutes(self, loguru_caplog, blocked_sweep):
        entered, _ = blocked_sweep
        assert fpmod.start_fingerprint_sweep(MagicMock()) is True
        assert entered.wait(5)
        self.clock.now += fpmod.SWEEP_STUCK_S
        assert fpmod.start_fingerprint_sweep(MagicMock()) is False
        assert "still waiting" not in loguru_caplog.text

    def test_a_sweep_starts_at_most_once_an_hour(self):
        done = threading.Event()
        with patch.object(fpmod, "sweep_fingerprint_cache", side_effect=lambda store: done.set() or 0) as swept:
            assert fpmod.start_fingerprint_sweep(MagicMock()) is True
            assert done.wait(5)
            assert fpmod._SWEEP_LOCK.acquire(timeout=5)  # finished
            fpmod._SWEEP_LOCK.release()
            self.clock.now += fpmod.SWEEP_MIN_GAP_S - 1
            assert fpmod.start_fingerprint_sweep(MagicMock()) is False
            self.clock.now += 1
            done.clear()
            assert fpmod.start_fingerprint_sweep(MagicMock()) is True
            assert done.wait(5)
        assert swept.call_count == 2

    def test_a_sweep_that_raises_is_logged_and_frees_the_next_one(self, loguru_caplog):
        with patch.object(fpmod, "sweep_fingerprint_cache", side_effect=OSError("disk I/O error")):
            assert fpmod.start_fingerprint_sweep(MagicMock()) is True
            assert fpmod._SWEEP_LOCK.acquire(timeout=5)
            fpmod._SWEEP_LOCK.release()
        assert "Couldn't clear old audio fingerprints" in loguru_caplog.text
