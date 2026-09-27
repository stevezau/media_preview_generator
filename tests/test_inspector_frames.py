"""Exact frames read from the video with ffmpeg (mocked), cached under a temp folder, never next to the video."""

from __future__ import annotations

import os
import subprocess
from unittest.mock import patch

import pytest

from media_preview_generator.inspector import frames
from media_preview_generator.inspector.frames import FramesBusyError, FramesError, exact_frames

FFMPEG = "/usr/bin/ffmpeg"


def _jpeg(i: int) -> bytes:
    return b"\xff\xd8\xff" + bytes([i]) * 16


@pytest.fixture(autouse=True)
def sdr_video(monkeypatch):
    """Every test video reads as SDR unless a test says otherwise (no real ffprobe runs)."""
    monkeypatch.setattr(frames, "_color_transfer", lambda path, ffmpeg: "bt709")


@pytest.fixture
def cache(tmp_path, monkeypatch):
    root = tmp_path / "cache"
    monkeypatch.setattr(frames, "cache_root", lambda: str(root))
    return root


@pytest.fixture
def video(tmp_path):
    folder = tmp_path / "media" / "Film (2020)"
    folder.mkdir(parents=True)
    path = folder / "Film (2020).mkv"
    path.write_bytes(b"video bytes")
    return str(path)


@pytest.fixture
def no_nice(monkeypatch):
    monkeypatch.setattr(frames.shutil, "which", lambda name: None)


def _ffmpeg_writing(n: int):
    """A ``frames._run`` stand-in that writes ``n`` JPEGs where the command's output pattern points."""

    def run(cmd, **kwargs):
        out_dir = os.path.dirname(cmd[-1])
        for i in range(1, n + 1):
            with open(os.path.join(out_dir, f"{i:02d}.jpg"), "wb") as f:
                f.write(_jpeg(i))
        return 0, b""

    return run


def _free_slots() -> int:
    taken = 0
    while frames._slots.acquire(blocking=False):
        taken += 1
    for _ in range(taken):
        frames._slots.release()
    return taken


class TestCommand:
    def test_command_runs_under_nice_when_nice_is_found(self, cache, video, monkeypatch):
        monkeypatch.setattr(frames.shutil, "which", lambda name: {"nice": "/usr/bin/nice"}.get(name))
        with patch("media_preview_generator.inspector.frames._run", side_effect=_ffmpeg_writing(3)) as run:
            exact_frames(video, start_ms=123_000, count=3, width=320, ffmpeg=FFMPEG)

        cmd = run.call_args.args[0]
        assert cmd[:4] == ["/usr/bin/nice", "-n", "10", FFMPEG]

    def test_command_has_no_nice_prefix_when_nice_is_missing(self, cache, video, no_nice):
        with patch("media_preview_generator.inspector.frames._run", side_effect=_ffmpeg_writing(3)) as run:
            exact_frames(video, start_ms=123_000, count=3, width=320, ffmpeg=FFMPEG)

        assert run.call_args.args[0][0] == FFMPEG

    def test_command_seeks_before_the_input_and_reads_count_frames_at_width(self, cache, video, no_nice):
        with patch("media_preview_generator.inspector.frames._run", side_effect=_ffmpeg_writing(5)) as run:
            exact_frames(video, start_ms=123_000, count=5, width=240, ffmpeg=FFMPEG)

        call = run.call_args
        cmd = call.args[0]
        assert cmd.index("-ss") < cmd.index("-i")
        assert cmd[cmd.index("-ss") + 1] == "123.000"
        assert cmd[cmd.index("-i") + 1] == f"file:{video}"
        assert "fps=1,scale=240:-2" in cmd[cmd.index("-vf") + 1]
        assert cmd[cmd.index("-frames:v") + 1] == "5"
        assert call.kwargs["timeout"] == frames.RUN_TIMEOUT_S
        assert cmd[cmd.index("-vf") + 1] == "fps=1,scale=240:-2,format=yuvj420p"

    def test_command_formats_milliseconds_in_the_seek(self, cache, video, no_nice):
        with patch("media_preview_generator.inspector.frames._run", side_effect=_ffmpeg_writing(1)) as run:
            exact_frames(video, start_ms=1_234, count=1, width=320, ffmpeg=FFMPEG)

        cmd = run.call_args.args[0]
        assert cmd[cmd.index("-ss") + 1] == "1.234"


class TestFrames:
    def test_returns_each_frame_one_second_apart_from_start(self, cache, video, no_nice):
        with patch("media_preview_generator.inspector.frames._run", side_effect=_ffmpeg_writing(3)):
            got = exact_frames(video, start_ms=60_000, count=3, width=320, ffmpeg=FFMPEG)

        assert got == [(60_000, _jpeg(1)), (61_000, _jpeg(2)), (62_000, _jpeg(3))]

    def test_fewer_frames_when_the_video_ends_first(self, cache, video, no_nice):
        with patch("media_preview_generator.inspector.frames._run", side_effect=_ffmpeg_writing(2)):
            got = exact_frames(video, start_ms=0, count=7, width=320, ffmpeg=FFMPEG)

        assert [t for t, _ in got] == [0, 1000]

    def test_second_identical_call_is_served_from_the_cache(self, cache, video, no_nice):
        with patch("media_preview_generator.inspector.frames._run", side_effect=_ffmpeg_writing(3)) as run:
            first = exact_frames(video, start_ms=5_000, count=3, width=320, ffmpeg=FFMPEG)
            second = exact_frames(video, start_ms=5_000, count=3, width=320, ffmpeg=FFMPEG)

        assert run.call_count == 1
        assert second == first

    @pytest.mark.parametrize("change", ["size", "mtime"])
    def test_changed_file_misses_the_cache(self, cache, video, no_nice, change):
        with patch("media_preview_generator.inspector.frames._run", side_effect=_ffmpeg_writing(3)) as run:
            exact_frames(video, start_ms=5_000, count=3, width=320, ffmpeg=FFMPEG)
            if change == "size":
                with open(video, "ab") as f:
                    f.write(b"more")
            else:
                st = os.stat(video)
                os.utime(video, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
            exact_frames(video, start_ms=5_000, count=3, width=320, ffmpeg=FFMPEG)

        assert run.call_count == 2

    @pytest.mark.parametrize(
        "other",
        [
            {"start_ms": 6_000, "count": 3, "width": 320},
            {"start_ms": 5_000, "count": 4, "width": 320},
            {"start_ms": 5_000, "count": 3, "width": 480},
        ],
    )
    def test_different_request_misses_the_cache(self, cache, video, no_nice, other):
        with patch("media_preview_generator.inspector.frames._run", side_effect=_ffmpeg_writing(3)) as run:
            exact_frames(video, start_ms=5_000, count=3, width=320, ffmpeg=FFMPEG)
            exact_frames(video, ffmpeg=FFMPEG, **other)

        assert run.call_count == 2

    def test_nothing_is_written_next_to_the_video(self, cache, video, no_nice):
        with patch("media_preview_generator.inspector.frames._run", side_effect=_ffmpeg_writing(3)):
            exact_frames(video, start_ms=0, count=3, width=320, ffmpeg=FFMPEG)

        assert os.listdir(os.path.dirname(video)) == [os.path.basename(video)]
        assert [n for n in os.listdir(cache) if not n.startswith(".")]
        assert not [n for n in os.listdir(cache) if n.startswith(".work-")]

    def test_ffmpeg_on_path_is_used_when_none_is_given(self, cache, video, monkeypatch):
        monkeypatch.setattr(frames.shutil, "which", lambda name: {"ffmpeg": "/opt/bin/ffmpeg"}.get(name))
        with patch("media_preview_generator.inspector.frames._run", side_effect=_ffmpeg_writing(1)) as run:
            exact_frames(video, start_ms=0, count=1, width=320)

        assert run.call_args.args[0][0] == "/opt/bin/ffmpeg"


class TestFailures:
    def test_timeout_is_a_frames_error_and_frees_the_slot(self, cache, video, no_nice):
        free_before = _free_slots()
        with (
            patch(
                "media_preview_generator.inspector.frames._run",
                side_effect=subprocess.TimeoutExpired(["ffmpeg"], frames.RUN_TIMEOUT_S),
            ),
            pytest.raises(FramesError, match="longer than"),
        ):
            exact_frames(video, start_ms=0, count=3, width=320, ffmpeg=FFMPEG)

        assert _free_slots() == free_before
        assert not [n for n in os.listdir(cache) if n.startswith(".work-")]

    def test_oserror_is_a_frames_error(self, cache, video, no_nice):
        with (
            patch("media_preview_generator.inspector.frames._run", side_effect=PermissionError("denied")),
            pytest.raises(FramesError, match="PermissionError"),
        ):
            exact_frames(video, start_ms=0, count=3, width=320, ffmpeg=FFMPEG)

    def test_no_output_is_a_frames_error_and_is_not_cached(self, cache, video, no_nice):
        failed = (1, b"line one\nInvalid data found\n")
        with patch("media_preview_generator.inspector.frames._run", return_value=failed) as run:
            with pytest.raises(FramesError, match="couldn't read frames"):
                exact_frames(video, start_ms=0, count=3, width=320, ffmpeg=FFMPEG)
            with pytest.raises(FramesError):
                exact_frames(video, start_ms=0, count=3, width=320, ffmpeg=FFMPEG)

        assert run.call_count == 2
        assert os.listdir(cache) == []

    def test_missing_ffmpeg_is_a_frames_error(self, cache, video, no_nice):
        with (
            patch("media_preview_generator.inspector.frames._run") as run,
            pytest.raises(FramesError, match="isn't installed"),
        ):
            exact_frames(video, start_ms=0, count=3, width=320, ffmpeg=None)

        run.assert_not_called()

    @pytest.mark.parametrize(
        ("path_kind", "start_ms", "count", "width"),
        [
            ("relative", 0, 3, 320),
            ("absolute", 0, 0, 320),
            ("absolute", 0, 15, 320),
            ("absolute", 0, 3, 300),
            ("absolute", -1, 3, 320),
        ],
    )
    def test_bad_arguments_raise_value_error(self, cache, video, no_nice, path_kind, start_ms, count, width):
        path = os.path.basename(video) if path_kind == "relative" else video
        with (
            patch("media_preview_generator.inspector.frames._run") as run,
            pytest.raises(ValueError),
        ):
            exact_frames(path, start_ms=start_ms, count=count, width=width, ffmpeg=FFMPEG)

        run.assert_not_called()

    def test_busy_when_every_slot_stays_taken(self, cache, video, no_nice, monkeypatch):
        monkeypatch.setattr(frames, "SLOT_WAIT_S", 0.05)
        held = 0
        try:
            for _ in range(frames.MAX_RUNNING):
                assert frames._slots.acquire(timeout=1)
                held += 1
            with (
                patch("media_preview_generator.inspector.frames._run") as run,
                pytest.raises(FramesBusyError),
            ):
                exact_frames(video, start_ms=0, count=3, width=320, ffmpeg=FFMPEG)
            run.assert_not_called()
        finally:
            for _ in range(held):
                frames._slots.release()


class TestPrune:
    def _old_answer(self, root, name: str, size: int, mtime: float) -> str:
        folder = root / name
        folder.mkdir(parents=True)
        (folder / "01.jpg").write_bytes(b"\x00" * size)
        os.utime(folder, (mtime, mtime))
        return str(folder)

    def test_oldest_answers_go_until_the_cache_fits_and_the_newest_stays(self, cache, video, no_nice, monkeypatch):
        oldest = self._old_answer(cache, "a" * 40, 100, 1_000_000)
        older = self._old_answer(cache, "b" * 40, 100, 2_000_000)
        # The new answer is 3 x 19 bytes; the cap fits it and nothing else.
        monkeypatch.setattr(frames, "CACHE_MAX_BYTES", 60)
        with patch("media_preview_generator.inspector.frames._run", side_effect=_ffmpeg_writing(3)):
            exact_frames(video, start_ms=0, count=3, width=320, ffmpeg=FFMPEG)

        left = os.listdir(cache)
        assert os.path.basename(oldest) not in left
        assert os.path.basename(older) not in left
        assert len(left) == 1
        assert sorted(os.listdir(cache / left[0])) == ["01.jpg", "02.jpg", "03.jpg"]

    def test_only_as_many_old_answers_go_as_needed(self, cache, video, no_nice, monkeypatch):
        oldest = self._old_answer(cache, "a" * 40, 100, 1_000_000)
        older = self._old_answer(cache, "b" * 40, 100, 2_000_000)
        monkeypatch.setattr(frames, "CACHE_MAX_BYTES", 200)
        with patch("media_preview_generator.inspector.frames._run", side_effect=_ffmpeg_writing(3)):
            exact_frames(video, start_ms=0, count=3, width=320, ffmpeg=FFMPEG)

        left = os.listdir(cache)
        assert os.path.basename(oldest) not in left
        assert os.path.basename(older) in left
        assert len(left) == 2

    def test_the_new_answer_stays_even_over_the_cap(self, cache, video, no_nice, monkeypatch):
        self._old_answer(cache, "a" * 40, 100, 1_000_000)
        monkeypatch.setattr(frames, "CACHE_MAX_BYTES", 1)
        with patch("media_preview_generator.inspector.frames._run", side_effect=_ffmpeg_writing(3)) as run:
            exact_frames(video, start_ms=0, count=3, width=320, ffmpeg=FFMPEG)
            exact_frames(video, start_ms=0, count=3, width=320, ffmpeg=FFMPEG)

        assert len(os.listdir(cache)) == 1
        assert run.call_count == 1


class TestHdrAndStalls:
    def test_an_hdr10_video_is_tone_mapped_with_the_previews_curve(self, cache, video, no_nice, monkeypatch):
        monkeypatch.setattr(frames, "_color_transfer", lambda path, ffmpeg: "smpte2084")
        with patch("media_preview_generator.inspector.frames._run", side_effect=_ffmpeg_writing(2)) as run:
            exact_frames(video, start_ms=0, count=2, width=320, ffmpeg=FFMPEG, tonemap="mobius")

        cmd = run.call_args.args[0]
        vf = cmd[cmd.index("-vf") + 1]
        assert vf.startswith("fps=1,scale=320:-2,zscale=t=linear:npl=100")
        assert "tonemap=mobius:desat=0" in vf
        assert run.call_count == 1

    def test_an_ffmpeg_without_zscale_reads_the_hdr_frames_again_plainly(self, cache, video, no_nice, monkeypatch):
        monkeypatch.setattr(frames, "_color_transfer", lambda path, ffmpeg: "arib-std-b67")
        writes = _ffmpeg_writing(2)
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd[cmd.index("-vf") + 1])
            if len(calls) == 1:
                return 1, b"No such filter: 'zscale'\n"
            return writes(cmd, **kwargs)

        with patch("media_preview_generator.inspector.frames._run", side_effect=run):
            out = exact_frames(video, start_ms=0, count=2, width=320, ffmpeg=FFMPEG)

        assert len(out) == 2
        assert "zscale" in calls[0]
        assert calls[1] == "fps=1,scale=320:-2,format=yuvj420p"

    def test_a_stalled_ffmpeg_is_killed_and_its_slot_freed_at_once(self, cache, video, no_nice, monkeypatch):
        killed = []

        class Stalled:
            returncode = None

            def communicate(self, timeout=None):
                raise frames.subprocess.TimeoutExpired(["ffmpeg"], timeout)

        monkeypatch.setattr(frames.subprocess, "Popen", lambda *a, **k: Stalled())
        monkeypatch.setattr(frames, "kill_and_collect", lambda proc, **kw: killed.append(kw) or False)
        before = _free_slots()
        with pytest.raises(FramesError, match="longer than 30 s"):
            exact_frames(video, start_ms=0, count=3, width=320, ffmpeg=FFMPEG)

        assert killed and killed[0]["wait_s"] == frames._KILL_WAIT_S
        assert killed[0]["reaper_name"] == frames._REAPER
        assert _free_slots() == before

    def test_a_cache_folder_that_is_a_link_is_refused(self, tmp_path, video, no_nice, monkeypatch):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        link = tmp_path / "cache-link"
        link.symlink_to(elsewhere)
        monkeypatch.setattr(frames, "cache_root", lambda: str(link))
        with (
            patch("media_preview_generator.inspector.frames._run") as run,
            pytest.raises(FramesError, match="isn't this app's own"),
        ):
            exact_frames(video, start_ms=0, count=3, width=320, ffmpeg=FFMPEG)

        run.assert_not_called()
        assert os.listdir(elsewhere) == []

    def test_the_cache_folder_is_private(self, cache, video, no_nice):
        with patch("media_preview_generator.inspector.frames._run", side_effect=_ffmpeg_writing(1)):
            exact_frames(video, start_ms=0, count=1, width=320, ffmpeg=FFMPEG)

        assert os.stat(cache).st_mode & 0o777 == 0o700
