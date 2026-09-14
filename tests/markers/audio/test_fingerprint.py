"""Chromaprint fingerprints: window, command, ffmpeg choice, caching, identity gate, cancel, ≤ 2 in parallel."""

from __future__ import annotations

import subprocess
import threading
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from media_preview_generator.markers.audio import POINT_S
from media_preview_generator.markers.audio import fingerprint as fpmod
from media_preview_generator.markers.models import FileIdentity
from media_preview_generator.markers.store import MarkerStore


@pytest.fixture(autouse=True)
def _fresh_chromaprint_cache():
    fpmod.has_chromaprint.cache_clear()
    yield
    fpmod.has_chromaprint.cache_clear()


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


def test_cancel_kills_ffmpeg():
    proc = _proc(hang=True)
    with patch.object(fpmod.subprocess, "Popen", return_value=proc):
        with pytest.raises(fpmod.FingerprintError, match="cancelled"):
            fpmod.compute_fingerprint("/m/a.mkv", 60_000, ffmpeg="ffmpeg", cancel_check=lambda: True)
    proc.kill.assert_called_once()


def _record(store, tmp_path, name="S01E01.mkv"):
    path = tmp_path / name
    path.write_bytes(b"x" * 10)
    st = path.stat()
    return store.upsert_file(
        FileIdentity(str(path), st.st_size, st.st_mtime_ns),
        duration_ms=300_000,
        season_key=str(tmp_path),
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
