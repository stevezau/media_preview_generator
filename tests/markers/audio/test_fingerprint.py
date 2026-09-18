"""Chromaprint fingerprints: window, command, ffmpeg choice, caching, identity gate, cancel, ≤ 2 in parallel."""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime
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


def test_command_is_the_spec_command_with_thread_cap():
    assert fpmod.fingerprint_command("/usr/lib/jellyfin-ffmpeg/ffmpeg", "/m/a.mkv", 462.5152) == [
        "/usr/lib/jellyfin-ffmpeg/ffmpeg", "-nostdin", "-v", "error", "-threads", "2",
        "-ss", "0", "-t", "462.515", "-i", "/m/a.mkv",
        "-vn", "-sn", "-dn", "-ac", "2", "-f", "chromaprint", "-algorithm", "1", "-fp_format", "raw", "-",
    ]  # fmt: skip


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
    compute.assert_called_once_with(rec.canonical_path, 300_000, ffmpeg="ffmpeg", cancel_check=None)
    stored = store.get_fingerprint(rec.id, "intro")
    assert (stored.start_s, stored.length_s, stored.algorithm) == (0.0, 105.0, 1)


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
    compute.assert_called_once_with(rec.canonical_path, rec.duration_ms, ffmpeg="ffmpeg", cancel_check=None)


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


def test_at_most_two_fingerprints_run_at_once(store, tmp_path):
    recs = [_record(store, tmp_path, f"S01E0{i}.mkv") for i in range(1, 6)]
    running, peak, guard = [0], [0], threading.Lock()

    def slow(*_a, **_kw):
        with guard:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.05)
        with guard:
            running[0] -= 1
        return np.array([1], dtype="<u4")

    with patch.object(fpmod, "compute_fingerprint", side_effect=slow):
        threads = [
            threading.Thread(target=fpmod.ensure_fingerprint, args=(store, r), kwargs={"ffmpeg": "f"}) for r in recs
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
    assert peak[0] == 2


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
    compute.assert_called_once_with(rec.canonical_path, rec.duration_ms, ffmpeg="ffmpeg", cancel_check=None)
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
