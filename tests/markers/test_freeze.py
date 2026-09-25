"""Freezing a running markers subprocess while everything is paused, the way previews freeze their FFmpeg.

The process is a real child (a small Python script counting into a file), so SIGSTOP and SIGCONT are exercised for real
and read back from ``/proc``.
"""

from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace

import pytest

from media_preview_generator.markers import freeze as freeze_mod
from media_preview_generator.markers.freeze import Freeze


def _counter(path: pathlib.Path, *, new_session: bool = True) -> subprocess.Popen:
    script = textwrap.dedent(f"""
        import os, time
        n = 0
        while True:
            n += 1
            open({str(path)!r} + ".tmp", "w").write(str(n))
            os.replace({str(path)!r} + ".tmp", {str(path)!r})
            time.sleep(0.02)
    """)
    return subprocess.Popen([sys.executable, "-c", script], start_new_session=new_session)


def _state(pid: int) -> str:
    return pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split(" ", 1)[0]


def _wait_for(condition, *, within_s: float = 5.0) -> bool:
    deadline = time.monotonic() + within_s
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.02)
    return condition()


def _count(path: pathlib.Path) -> int:
    try:
        return int(path.read_text() or 0)
    except (FileNotFoundError, ValueError):
        return 0


@pytest.fixture
def signals(monkeypatch):
    """Every signal the module sends to a process group or a process, still sent."""
    sent: list[tuple[str, int, int]] = []
    real_killpg = os.killpg

    def killpg(pgid, sig):
        sent.append(("group", pgid, sig))
        real_killpg(pgid, sig)

    monkeypatch.setattr(freeze_mod.os, "killpg", killpg)
    return sent


@pytest.fixture
def child(tmp_path):
    procs: list[subprocess.Popen] = []

    def start(**kwargs) -> tuple[subprocess.Popen, pathlib.Path]:
        path = tmp_path / f"count{len(procs)}"
        proc = _counter(path, **kwargs)
        procs.append(proc)
        assert _wait_for(lambda: _count(path) > 0)
        return proc, path

    yield start
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
            proc.wait(5)


class TestHold:
    def test_the_running_group_is_stopped_while_paused_and_goes_on_from_there_on_resume(self, child, signals):
        proc, path = child()
        paused = threading.Event()
        paused.set()
        freeze = Freeze(paused.is_set, poll_s=0.02)
        holder = threading.Thread(target=freeze.hold, args=(proc,), kwargs={"name": "a.mkv"})
        holder.start()
        assert _wait_for(lambda: _state(proc.pid) == "T")
        frozen_at = _count(path)
        time.sleep(0.3)
        assert _count(path) == frozen_at  # stopped where it was, nothing lost and nothing done
        assert holder.is_alive()  # the worker waits with it
        paused.clear()
        holder.join(5)
        assert not holder.is_alive()
        assert _state(proc.pid) != "T"
        assert _wait_for(lambda: _count(path) > frozen_at)
        assert signals == [("group", proc.pid, signal.SIGSTOP), ("group", proc.pid, signal.SIGCONT)]

    def test_nothing_is_signalled_and_nothing_waits_when_nothing_is_paused(self, child, signals):
        proc, _ = child()
        freeze = Freeze(lambda: False)
        started = time.monotonic()
        assert freeze.hold(proc, name="a.mkv") == 0.0
        assert time.monotonic() - started < 0.1
        assert signals == [] and freeze.held_s == 0.0

    def test_no_check_is_never_paused(self, child, signals):
        proc, _ = child()
        assert Freeze(None).hold(proc) == 0.0
        assert signals == []

    def test_a_cancel_while_paused_continues_the_group_so_the_caller_can_kill_it(self, child, signals):
        proc, _ = child()
        cancelled = threading.Event()
        freeze = Freeze(lambda: True, poll_s=0.02)
        threading.Timer(0.2, cancelled.set).start()
        freeze.hold(proc, cancel_check=cancelled.is_set, name="a.mkv")
        assert signals == [("group", proc.pid, signal.SIGSTOP), ("group", proc.pid, signal.SIGCONT)]
        assert _state(proc.pid) != "T"

    def test_a_process_sharing_the_apps_group_is_signalled_alone(self, child, signals):
        # Never the app's own process group: that would stop the web app itself.
        proc, path = child(new_session=False)
        paused = threading.Event()
        paused.set()
        freeze = Freeze(paused.is_set, poll_s=0.02)
        holder = threading.Thread(target=freeze.hold, args=(proc,))
        holder.start()
        assert _wait_for(lambda: _state(proc.pid) == "T")
        paused.clear()
        holder.join(5)
        assert _wait_for(lambda: _state(proc.pid) != "T")
        assert signals == []

    def test_a_process_that_already_exited_is_not_signalled_but_the_caller_still_waits(self, signals):
        proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        proc.wait(5)
        paused = threading.Event()
        paused.set()
        threading.Timer(0.2, paused.clear).start()
        held = Freeze(paused.is_set, poll_s=0.02).hold(proc)
        assert held >= 0.15
        assert signals == []

    def test_with_no_process_it_only_waits_for_the_resume(self, signals):
        paused = threading.Event()
        paused.set()
        threading.Timer(0.2, paused.clear).start()
        assert Freeze(paused.is_set, poll_s=0.02).hold(None) >= 0.15
        assert signals == []


class TestClock:
    def test_the_clock_leaves_out_the_time_held(self, monkeypatch):
        now = [100.0]
        paused = [True]

        def sleep(seconds):
            now[0] += seconds
            if now[0] >= 130.0:
                paused[0] = False

        monkeypatch.setattr(freeze_mod, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=sleep))
        freeze = Freeze(lambda: paused[0], poll_s=1.0)
        assert freeze.clock() == 100.0
        assert freeze.hold(None) == 30.0
        now[0] += 5.0
        assert freeze.held_s == 30.0
        assert freeze.clock() == 105.0  # 35 s passed, 30 of them paused

    def test_of_keeps_one_freeze_so_its_time_held_adds_up_across_decodes(self):
        freeze = Freeze(lambda: False)
        assert Freeze.of(freeze) is freeze
        wrapped = Freeze.of(lambda: True)
        assert isinstance(wrapped, Freeze) and wrapped()
        assert not Freeze.of(None)()
