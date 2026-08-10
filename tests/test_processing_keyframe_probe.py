"""Tests for the keyframe-probe and duplicate-thumbnail helpers added
to fix issue #238 (duplicate frames in BIF when ``-skip_frame:v nokey``
runs against a file whose keyframe spacing is larger than the user's
thumbnail interval).

The unit tests cover the helpers in isolation (ffprobe-parser + adjacent-
hash counter).  A separate integration block at the bottom exercises the
wiring inside ``generate_images`` itself, asserting that the right
retry-cascade tier fires for each probe/validator branch:

1. probe inconclusive → first FFmpeg call uses ``use_skip=False``,
2. probe says unsafe → first FFmpeg call uses ``use_skip=False`` + WARN,
3. probe clears + post-extract validator detects duplicates → first
   FFmpeg call uses ``use_skip=True``, then a second call fires with
   ``use_skip=False`` (the validator-driven retry).
4. gap within the #256 tolerance (interval × 1.064) → keeps ``use_skip=True``,
   no slow-path WARN.
5. gap just beyond the tolerance → ``use_skip=False`` + WARN.

The integration tests deliberately do NOT use the autouse fixture from
``test_media_processing.py`` (different module, different scope), so the
real helpers and the real generate_images flow execute end-to-end with
only the subprocess + filesystem boundary mocked.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest
from loguru import logger

from media_preview_generator.processing.generator import (
    KEYFRAME_DUPLICATE_TOLERANCE,
    KEYFRAME_PROBE_MIN_SPACING_FACTOR,
    KEYFRAME_PROBE_WINDOWS,
    _has_duplicate_thumbnails,
    _keyframe_probe_window_length,
    _max_gap_within_windows,
    _probe_max_keyframe_gap,
    _video_duration_seconds,
    generate_images,
)


@pytest.fixture
def loguru_caplog(caplog):
    """Bridge loguru → pytest's caplog so the integration tests can
    assert on the user-facing WARN messages.  Mirrors the helper in
    ``test_version_check.py``.
    """

    class _PropagateHandler(logging.Handler):
        def emit(self, record):  # pragma: no cover — handler glue
            logging.getLogger(record.name).handle(record)

    handler_id = logger.add(_PropagateHandler(), level="DEBUG", format="{message}")
    caplog.set_level(logging.DEBUG)
    try:
        yield caplog
    finally:
        logger.remove(handler_id)


# ---------------------------------------------------------------------------
# _probe_max_keyframe_gap
# ---------------------------------------------------------------------------


def _ffprobe_stdout(rows: list[tuple[float, str]]) -> str:
    """Render synthetic ffprobe csv=p=0 output for ``packet=pts_time,flags``.

    ``rows`` is a list of (pts_time, flags) — e.g. ``(0.0, "K_")``,
    ``(0.042, "__")``.  Mirrors exactly what FFprobe emits in production.
    """
    return "\n".join(f"{ts:.6f},{flags}" for ts, flags in rows) + "\n"


def _mock_ffprobe(returncode: int, stdout: str) -> MagicMock:
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = ""
    return proc


def test_probe_returns_max_gap_with_dense_keyframes():
    """Keyframes every ~1 s — max gap is small."""
    rows = [(t, "K_") for t in [0.0, 1.0, 2.0, 3.0, 4.0]]
    with patch("media_preview_generator.processing.generator.subprocess.run") as mock_run:
        mock_run.return_value = _mock_ffprobe(0, _ffprobe_stdout(rows))
        gap = _probe_max_keyframe_gap("/fake/video.mkv")
    assert gap == pytest.approx(1.0)


def test_probe_returns_max_gap_with_sparse_keyframes():
    """The Dhurandhar pattern: irregular keyframes, max gap = 8.7s."""
    rows = [(t, "K_") for t in [0.0, 0.917, 5.0, 10.0, 18.708]]
    with patch("media_preview_generator.processing.generator.subprocess.run") as mock_run:
        mock_run.return_value = _mock_ffprobe(0, _ffprobe_stdout(rows))
        gap = _probe_max_keyframe_gap("/fake/video.mkv")
    assert gap == pytest.approx(8.708, abs=0.01)


def test_probe_ignores_non_keyframe_packets():
    """Probe must only consider rows whose flags contain 'K'."""
    rows = [
        (0.0, "K_"),
        (0.042, "__"),
        (0.083, "__"),
        (1.0, "K_"),
        (1.042, "__"),
        (10.0, "K_"),
    ]
    with patch("media_preview_generator.processing.generator.subprocess.run") as mock_run:
        mock_run.return_value = _mock_ffprobe(0, _ffprobe_stdout(rows))
        gap = _probe_max_keyframe_gap("/fake/video.mkv")
    # max gap is between keyframes at 1.0 and 10.0 = 9.0s
    assert gap == pytest.approx(9.0)


def test_probe_returns_none_on_ffprobe_error():
    """Non-zero return code means we can't trust the output."""
    with patch("media_preview_generator.processing.generator.subprocess.run") as mock_run:
        mock_run.return_value = _mock_ffprobe(1, "")
        assert _probe_max_keyframe_gap("/fake/video.mkv") is None


def test_probe_returns_none_on_timeout():
    """ffprobe hangs → callers must treat it as unsafe."""
    with patch("media_preview_generator.processing.generator.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="ffprobe", timeout=120)
        assert _probe_max_keyframe_gap("/fake/video.mkv") is None


def test_probe_returns_none_on_oserror():
    """ffprobe missing from PATH → still safe (returns None)."""
    with patch("media_preview_generator.processing.generator.subprocess.run") as mock_run:
        mock_run.side_effect = FileNotFoundError("ffprobe")
        assert _probe_max_keyframe_gap("/fake/video.mkv") is None


def test_probe_returns_none_when_no_keyframes():
    """Output has packets but none are keyframes — can't compute a gap."""
    rows = [(t, "__") for t in [0.0, 0.042, 0.083, 0.125]]
    with patch("media_preview_generator.processing.generator.subprocess.run") as mock_run:
        mock_run.return_value = _mock_ffprobe(0, _ffprobe_stdout(rows))
        assert _probe_max_keyframe_gap("/fake/video.mkv") is None


def test_probe_returns_none_with_single_keyframe():
    """Need at least 2 keyframes to compute a gap."""
    rows = [(0.0, "K_")] + [(t, "__") for t in [0.042, 0.083]]
    with patch("media_preview_generator.processing.generator.subprocess.run") as mock_run:
        mock_run.return_value = _mock_ffprobe(0, _ffprobe_stdout(rows))
        assert _probe_max_keyframe_gap("/fake/video.mkv") is None


def test_probe_handles_garbage_lines_gracefully():
    """Malformed lines must be skipped, not crash the parser."""
    stdout = "0.000000,K_\nthis is not a row\n,K_\n5.0,K_\n"
    with patch("media_preview_generator.processing.generator.subprocess.run") as mock_run:
        mock_run.return_value = _mock_ffprobe(0, stdout)
        gap = _probe_max_keyframe_gap("/fake/video.mkv")
    assert gap == pytest.approx(5.0)


def test_probe_handles_compound_flags():
    """Real FFprobe sometimes emits 'K__', 'K_D', etc. — all are keyframes."""
    rows = [
        (0.0, "K__"),
        (3.0, "K_D"),
        (10.0, "K_"),
    ]
    with patch("media_preview_generator.processing.generator.subprocess.run") as mock_run:
        mock_run.return_value = _mock_ffprobe(0, _ffprobe_stdout(rows))
        gap = _probe_max_keyframe_gap("/fake/video.mkv")
    assert gap == pytest.approx(7.0)


def test_probe_invokes_ffprobe_with_correct_args():
    """The command shape is part of the contract — packet listing on the
    first video stream, no decode.

    Assert paired flags by adjacency so a reorder regression (e.g. moving
    ``-select_streams`` next to a different value) trips the test.  The
    timeout / check kwargs are also part of the contract: ffprobe must
    be bounded and must NOT raise on non-zero rc (the caller treats it
    as "unsafe" and falls through to the slow path).
    """
    with patch("media_preview_generator.processing.generator.subprocess.run") as mock_run:
        mock_run.return_value = _mock_ffprobe(0, _ffprobe_stdout([(0.0, "K_"), (1.0, "K_")]))
        _probe_max_keyframe_gap("/some/file.mkv")
    args = mock_run.call_args[0][0]
    assert args[0] == "ffprobe"
    assert args[args.index("-select_streams") + 1] == "v:0"
    assert args[args.index("-show_entries") + 1] == "packet=pts_time,flags"
    assert args[args.index("-of") + 1] == "csv=p=0"
    assert "/some/file.mkv" in args
    # Without duration + gap_limit there's nothing to size windows from, so
    # this call must fall back to the whole-file scan (discussion #283).
    assert "-read_intervals" not in args
    # Boundary contract: bounded timeout, never raises on non-zero rc.
    assert mock_run.call_args.kwargs["timeout"] == 120
    assert mock_run.call_args.kwargs["check"] is False


# ---------------------------------------------------------------------------
# _probe_max_keyframe_gap — sampled mode (discussion #283)
# ---------------------------------------------------------------------------
#
# The full-file scan above reads every byte of the container to reach the
# video packets (measured: 18.1 GB read on an 18.2 GB MKV), which on a large
# file blew past the 120 s timeout and sent a perfectly fast-path-able file
# down the slow path.  With duration and gap_limit known the probe seeks to a
# spread of short windows instead.  These tests pin that the windows are
# actually requested, that the between-window jumps are not mistaken for
# keyframe gaps, and that short files still get the exact whole-file answer.


def _sampled(stdout: str, *, duration_s: float = 7200.0, gap_limit: float = 2.13):
    """Run the probe in sampled mode over synthetic ffprobe output."""
    with patch("media_preview_generator.processing.generator.subprocess.run") as mock_run:
        mock_run.return_value = _mock_ffprobe(0, stdout)
        gap = _probe_max_keyframe_gap("/fake/video.mkv", duration_s=duration_s, gap_limit=gap_limit)
    return gap, mock_run.call_args[0][0]


def test_sampled_probe_requests_read_intervals():
    """Duration + gap_limit present → ffprobe is asked for a fixed number
    of windows rather than the whole file."""
    _, args = _sampled(_ffprobe_stdout([(0.0, "K_"), (2.0, "K_")]))
    assert "-read_intervals" in args, "sampled mode must not scan the whole file"
    intervals = args[args.index("-read_intervals") + 1]
    assert len(intervals.split(",")) == KEYFRAME_PROBE_WINDOWS
    # Each window is "<start>%+<len>" — the "%+" form seeks then reads a
    # duration, which is what keeps the read cheap.
    assert all("%+" in window for window in intervals.split(","))


def test_sampled_probe_windows_stay_inside_the_runtime():
    """A window starting past EOF would come back empty and read as a
    false 'keyframes are sparse here' signal."""
    duration = 7200.0
    _, args = _sampled(_ffprobe_stdout([(0.0, "K_"), (2.0, "K_")]), duration_s=duration)
    intervals = args[args.index("-read_intervals") + 1].split(",")
    starts = [float(window.split("%")[0]) for window in intervals]
    length = float(intervals[0].split("%+")[1])
    assert starts[0] > 0, "first window must not start at 0"
    assert max(starts) + length < duration, "last window must end before EOF"
    assert starts == sorted(starts)


def test_sampled_probe_window_length_scales_with_gap_limit():
    """Windows must be long enough to hold several keyframes of a
    compliant file, or a single-keyframe window is ambiguous."""
    _, args = _sampled(_ffprobe_stdout([(0.0, "K_"), (2.0, "K_")]), gap_limit=10.64)
    length = float(args[args.index("-read_intervals") + 1].split("%+")[1].split(",")[0])
    assert length >= 10.64 * 2, "window must fit at least two gap_limit-spaced keyframes"


@pytest.mark.parametrize("interval", range(1, 61))
def test_window_length_keeps_its_guarantees_at_every_allowed_interval(interval):
    """The two properties the sparse rule rests on, across the whole range
    the UI allows (config/validation.py caps the interval at 60 s).

    An earlier revision clamped the window at 60 s, which silently broke
    both: from interval 29 a compliant file could put a lone keyframe in a
    window and be falsely slow-decoded, and from interval 57 the probe could
    not report a gap above ``gap_limit`` at all — meaning no file, however
    sparse, could ever be denied the fast path.
    """
    gap_limit = interval / (1 - KEYFRAME_DUPLICATE_TOLERANCE)
    window = _keyframe_probe_window_length(gap_limit)
    assert window >= 2 * gap_limit, "a compliant file must land >= 2 keyframes per window"
    assert window > gap_limit, "probe can never report a gap above one window — sparse verdict must stay reachable"


@pytest.mark.parametrize("interval", [1, 2, 10, 20, 40, 60])
def test_sampled_windows_never_merge_into_each_other(interval):
    """Regression: adjacent windows must stay far enough apart that the
    jump between them exceeds a window length, or the clustering in
    _max_gap_within_windows reads that boundary as a keyframe gap.

    Concretely, a 13-minute file at the default 10 s interval used to
    report a 30 s gap and slow-decode — the exact #283 symptom this change
    exists to remove.  Walks the shortest duration that still samples.
    """
    gap_limit = interval / (1 - KEYFRAME_DUPLICATE_TOLERANCE)
    window = _keyframe_probe_window_length(gap_limit)
    shortest_sampled = window * (KEYFRAME_PROBE_WINDOWS + 1) * KEYFRAME_PROBE_MIN_SPACING_FACTOR

    for duration in (shortest_sampled * 1.001, shortest_sampled * 1.5, shortest_sampled * 10):
        spacing = duration / (KEYFRAME_PROBE_WINDOWS + 1)
        assert spacing - window > window, (
            f"windows merge at interval={interval} duration={duration:.0f}: spacing={spacing:.1f} window={window:.1f}"
        )


def test_short_file_in_the_old_merge_band_falls_back_to_full_scan():
    """A 13-minute file at the default 10 s interval sits in the band where
    windows would crowd together — it must take the exact whole-file scan
    rather than a sampled reading it cannot trust."""
    _, args = _sampled(
        _ffprobe_stdout([(0.0, "K_"), (2.0, "K_")]),
        duration_s=800.0,
        gap_limit=10 / (1 - KEYFRAME_DUPLICATE_TOLERANCE),
    )
    assert "-read_intervals" not in args


def test_sampled_probe_ignores_jumps_between_windows():
    """The regression this whole mode risks: timestamps from two different
    windows are ~minutes apart, and that jump is an artefact of sampling —
    measuring it would send every sampled file down the slow path."""
    stdout = _ffprobe_stdout(
        [(600.0, "K_"), (602.0, "K_"), (604.0, "K_")]  # window 1
        + [(3600.0, "K_"), (3602.0, "K_"), (3604.0, "K_")]  # window 2, ~50 min later
    )
    gap, _ = _sampled(stdout)
    assert gap == pytest.approx(2.0), "cross-window jump must not count as a keyframe gap"


def test_sampled_probe_reports_real_gap_inside_a_window():
    """A genuinely sparse file is still caught — the gap lives inside one
    window, so it survives the cross-window filter."""
    stdout = _ffprobe_stdout([(600.0, "K_"), (609.0, "K_")] + [(3600.0, "K_"), (3602.0, "K_")])
    gap, _ = _sampled(stdout, gap_limit=10.64)
    assert gap == pytest.approx(9.0)


def test_sampled_probe_treats_lone_keyframe_windows_as_sparse():
    """One keyframe per window means the gap there exceeds the window —
    which is by construction beyond gap_limit.  Must not be read as
    'no data', which would clear the fast path."""
    stdout = _ffprobe_stdout([(600.0, "K_"), (1200.0, "K_"), (1800.0, "K_")])
    gap, args = _sampled(stdout)
    length = float(args[args.index("-read_intervals") + 1].split("%+")[1].split(",")[0])
    assert gap is not None
    assert gap >= length > 2.13, "lone-keyframe windows must yield an unsafe verdict"


def test_sampled_probe_returns_none_when_no_keyframes_found():
    """Seek landed nowhere useful (damaged index) → no verdict, caller
    takes the slow path."""
    gap, _ = _sampled("")
    assert gap is None


def test_short_file_falls_back_to_full_scan():
    """A file barely longer than the sample windows gains nothing from
    seeking — scan it exactly instead."""
    _, args = _sampled(_ffprobe_stdout([(0.0, "K_"), (2.0, "K_")]), duration_s=120.0)
    assert "-read_intervals" not in args


def test_zero_duration_falls_back_to_full_scan():
    """MediaInfo returning 0/None duration must not produce a degenerate
    window layout."""
    _, args = _sampled(_ffprobe_stdout([(0.0, "K_"), (2.0, "K_")]), duration_s=0.0)
    assert "-read_intervals" not in args


def test_sampled_probe_matches_full_scan_on_evenly_spaced_keyframes():
    """Cross-check the two modes agree on the same synthetic file — the
    property the whole change rests on."""
    rows = [(float(t), "K_") for t in range(0, 7000, 2)]
    with patch("media_preview_generator.processing.generator.subprocess.run") as mock_run:
        mock_run.return_value = _mock_ffprobe(0, _ffprobe_stdout(rows))
        full = _probe_max_keyframe_gap("/fake/video.mkv")
        sampled = _probe_max_keyframe_gap("/fake/video.mkv", duration_s=7000.0, gap_limit=2.13)
    assert sampled == pytest.approx(full)


# ---------------------------------------------------------------------------
# Sampled probe against REAL ffprobe
# ---------------------------------------------------------------------------
#
# Everything above mocks subprocess.run, so the rows are perfectly clean and
# de-duplicated — which is exactly the shape real ffprobe does NOT produce
# when several sample windows land in one GOP.  The seek-rewind duplicate bug
# was structurally unrepresentable in the mocked tests and only showed up
# against the real binary, so these drive it end to end on generated files.


def _has_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


needs_ffmpeg = pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg/ffprobe not installed")


# Fixtures are rendered per test, so they're tuned to be cheap: tiny frames at
# a low rate, ultrafast preset.  Runtime is deliberately just over the shortest
# duration that still samples at interval=1 (window 15 s x 13 x 2.5 = 487.5 s) —
# any shorter and the probe would fall back to a full scan and prove nothing.
_REAL_PROBE_DURATION_S = 520
_REAL_PROBE_INTERVAL = 1


def _make_test_video(path: Path, duration_s: int, gop_s: int, fps: int = 5) -> str:
    """Render a synthetic video with keyframes exactly ``gop_s`` apart."""
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=size=64x48:rate={fps}:duration={duration_s}",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-pix_fmt", "yuv420p",
            "-g", str(gop_s * fps), "-keyint_min", str(gop_s * fps), "-sc_threshold", "0",
            str(path),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return str(path)


@needs_ffmpeg
@pytest.mark.parametrize(
    ("gop_s", "expect"),
    [
        # Keyframes inside the interval → fast path survives sampling.
        (1, "fast"),
        # GOP wider than the interval → slow path.
        (10, "slow"),
        # The seek-rewind case: GOP wider than twice the window spacing
        # (520/13 = 40 s), so several windows land in the same GOP and ffprobe
        # re-emits that one keyframe per window.  Before de-duplication those
        # repeats clustered at distance 0, the sparse rule never fired, and the
        # probe reported a 0.0 s gap — clearing the fast path for a 100 s GOP.
        (100, "slow"),
    ],
)
def test_sampled_verdict_matches_full_scan_on_real_files(tmp_path, gop_s, expect):
    """The property the whole change rests on: sampling reaches the same
    fast/slow decision as reading the entire file — driven through the real
    ffprobe binary, whose output shape the mocked tests cannot reproduce."""
    video = _make_test_video(tmp_path / f"gop{gop_s}.mp4", _REAL_PROBE_DURATION_S, gop_s)
    gap_limit = _REAL_PROBE_INTERVAL / (1 - KEYFRAME_DUPLICATE_TOLERANCE)

    full = _probe_max_keyframe_gap(video)
    sampled = _probe_max_keyframe_gap(video, duration_s=float(_REAL_PROBE_DURATION_S), gap_limit=gap_limit)

    def verdict(gap):
        return "slow" if (gap is None or gap > gap_limit) else "fast"

    assert verdict(full) == expect, f"full scan disagreed with the fixture's intent (gap={full})"
    assert verdict(sampled) == verdict(full), (
        f"sampled probe disagreed with the full scan: sampled={sampled} full={full} gap_limit={gap_limit:.2f}"
    )


@needs_ffmpeg
def test_sampled_probe_asks_real_ffprobe_for_a_fraction_of_the_runtime(tmp_path):
    """The point of the change.  Asserting on the span requested rather than
    wall-clock keeps it meaningful on a warm cache and on any storage speed."""
    video = _make_test_video(tmp_path / "throughput.mp4", _REAL_PROBE_DURATION_S, 1)
    gap_limit = _REAL_PROBE_INTERVAL / (1 - KEYFRAME_DUPLICATE_TOLERANCE)

    with patch("media_preview_generator.processing.generator.subprocess.run", wraps=subprocess.run) as spy:
        gap = _probe_max_keyframe_gap(video, duration_s=float(_REAL_PROBE_DURATION_S), gap_limit=gap_limit)
    argv = spy.call_args[0][0]
    intervals = argv[argv.index("-read_intervals") + 1].split(",")
    sampled_span = float(intervals[0].split("%+")[1]) * len(intervals)

    assert gap is not None, "the sampled probe must still reach a verdict"
    assert sampled_span < _REAL_PROBE_DURATION_S * 0.5, (
        f"sampling must cover well under half the runtime, covered {sampled_span:.0f}s"
    )


# ---------------------------------------------------------------------------
# _max_gap_within_windows
# ---------------------------------------------------------------------------


def test_max_gap_within_windows_empty_returns_none():
    assert _max_gap_within_windows([], 30.0) is None


def test_max_gap_within_windows_single_cluster():
    assert _max_gap_within_windows([0.0, 1.0, 3.0], 30.0) == pytest.approx(2.0)


def test_max_gap_within_windows_sorts_unordered_input():
    """ffprobe emits packets in decode order, so pts can arrive out of
    sequence on B-frame content — a negative 'gap' would clear the fast
    path for a file that shouldn't get it."""
    assert _max_gap_within_windows([3.0, 0.0, 1.0], 30.0) == pytest.approx(2.0)


def test_max_gap_within_windows_lone_keyframe_returns_window_length():
    assert _max_gap_within_windows([0.0, 500.0], 30.0) == pytest.approx(30.0)


def test_max_gap_within_windows_lone_keyframe_dominates_measured_gaps():
    """One window measured a 40 s gap, another held a lone keyframe whose
    real gap is unmeasurable but necessarily larger.  The verdict must come
    from the sparse window, not the one we happened to measure."""
    assert _max_gap_within_windows([0.0, 40.0, 5000.0], 60.0) == pytest.approx(60.0)


def test_max_gap_within_windows_ignores_seek_rewind_duplicates():
    """Regression, and the one failure direction that matters.

    ``-read_intervals`` seeks to the keyframe *preceding* each window start,
    so when several windows land inside one GOP, ffprobe re-emits that same
    keyframe once per window.  Those repeats sit 0 s apart, so they cluster
    together, the lone-keyframe rule never fires, and the largest measured
    gap collapses to 0.0 — clearing the fast path for a file with 300 s
    keyframes.  Verified against real ffprobe on a 1500 s / 300 s-GOP file
    before the de-duplication went in.
    """
    repeated = [0.0, 0.0, 300.0, 300.0, 300.0, 600.0, 600.0, 900.0, 900.0]
    assert _max_gap_within_windows(repeated, 60.0) == pytest.approx(60.0)


def test_max_gap_within_windows_dedupes_without_hiding_a_real_gap():
    """De-duplication must not swallow a genuine measurement: the same
    window's distinct keyframes still produce their real spacing."""
    assert _max_gap_within_windows([600.0, 600.0, 609.0, 609.0], 31.9) == pytest.approx(9.0)


def test_max_gap_within_windows_measured_gap_never_exceeds_window():
    """Clusters split on jumps larger than a window, so a 55 s separation
    under a 30 s window is a window boundary — not a keyframe gap."""
    assert _max_gap_within_windows([0.0, 55.0, 5000.0], 30.0) == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# _video_duration_seconds
# ---------------------------------------------------------------------------


def test_video_duration_seconds_converts_ms():
    info = MagicMock()
    info.video_tracks = [MagicMock(duration=7200000)]
    assert _video_duration_seconds(info) == pytest.approx(7200.0)


def test_video_duration_seconds_parses_string_duration():
    """Some MediaInfo builds report duration as a str."""
    info = MagicMock()
    info.video_tracks = [MagicMock(duration="7200000")]
    assert _video_duration_seconds(info) == pytest.approx(7200.0)


@pytest.mark.parametrize("tracks", [[], None])
def test_video_duration_seconds_none_without_video_tracks(tracks):
    info = MagicMock()
    info.video_tracks = tracks
    assert _video_duration_seconds(info) is None


def test_video_duration_seconds_none_on_unparseable_duration():
    info = MagicMock()
    info.video_tracks = [MagicMock(duration="not-a-number")]
    assert _video_duration_seconds(info) is None


def test_video_duration_seconds_none_on_zero():
    info = MagicMock()
    info.video_tracks = [MagicMock(duration=0)]
    assert _video_duration_seconds(info) is None


# ---------------------------------------------------------------------------
# _has_duplicate_thumbnails
# ---------------------------------------------------------------------------


def _write_jpgs(folder: Path, payloads: list[bytes]) -> None:
    """Write ``img-NNNNNN.jpg`` files with the given byte payloads."""
    folder.mkdir(parents=True, exist_ok=True)
    for i, data in enumerate(payloads, start=1):
        (folder / f"img-{i:06d}.jpg").write_bytes(data)


def test_no_duplicates_returns_false(tmp_path):
    """Every JPG distinct — well below the default tolerance."""
    _write_jpgs(tmp_path, [f"unique-{i}".encode() for i in range(100)])
    assert _has_duplicate_thumbnails(str(tmp_path)) is False


def test_heavy_run_length_duplicates_returns_true(tmp_path):
    """The Dhurandhar signature: many adjacent dupes."""
    payloads: list[bytes] = []
    for i in range(50):
        payloads.append(f"frame-{i}".encode())
        payloads.append(f"frame-{i}".encode())  # dupe of previous
    _write_jpgs(tmp_path, payloads)
    assert _has_duplicate_thumbnails(str(tmp_path)) is True


def test_below_threshold_returns_false(tmp_path):
    """3% adjacent dupes — under the 6% default threshold."""
    payloads = [f"frame-{i}".encode() for i in range(100)]
    payloads[10] = payloads[9]
    payloads[30] = payloads[29]
    payloads[50] = payloads[49]
    # 3 dupes out of 100 = 3%
    _write_jpgs(tmp_path, payloads)
    assert _has_duplicate_thumbnails(str(tmp_path)) is False


def test_above_threshold_returns_true(tmp_path):
    """7% adjacent dupes — over the 6% default threshold."""
    payloads = [f"frame-{i}".encode() for i in range(100)]
    for idx in range(1, 15, 2):
        payloads[idx] = payloads[idx - 1]
    # 7 dupes out of 100 = 7%
    _write_jpgs(tmp_path, payloads)
    assert _has_duplicate_thumbnails(str(tmp_path)) is True


def test_empty_folder_returns_false(tmp_path):
    """No JPGs to check — definitely not "more than threshold" duplicates."""
    assert _has_duplicate_thumbnails(str(tmp_path)) is False


def test_single_jpg_returns_false(tmp_path):
    """One image can't have an adjacent duplicate."""
    _write_jpgs(tmp_path, [b"only-one"])
    assert _has_duplicate_thumbnails(str(tmp_path)) is False


def test_only_glob_pattern_img_dash_is_considered(tmp_path):
    """The helper looks at ``img-*.jpg`` specifically — files renamed to
    the timestamp pattern (already-published runs) must not be scanned."""
    # Renamed files (post-rename pattern) shouldn't even be visible.
    (tmp_path / "0000000000.jpg").write_bytes(b"renamed-1")
    (tmp_path / "0000000002.jpg").write_bytes(b"renamed-1")  # dupe but ignored
    # No img-*.jpg at all → no pairs → returns False.
    assert _has_duplicate_thumbnails(str(tmp_path)) is False


def test_custom_threshold_respected(tmp_path):
    """Caller-supplied threshold flips the verdict accordingly."""
    payloads = [f"frame-{i}".encode() for i in range(100)]
    for idx in range(1, 11, 2):
        payloads[idx] = payloads[idx - 1]
    # 5 dupes out of 100 = 5%
    _write_jpgs(tmp_path, payloads)
    assert _has_duplicate_thumbnails(str(tmp_path), threshold=0.10) is False
    assert _has_duplicate_thumbnails(str(tmp_path), threshold=0.03) is True


def test_dupe_detection_matches_md5_signature(tmp_path):
    """Byte-identical files must hash the same and count as a dupe; a
    one-byte difference must not."""
    a = hashlib.md5(b"a" * 5000).digest()
    b = hashlib.md5(b"a" * 4999 + b"b").digest()
    assert a != b  # sanity
    payloads = [b"a" * 5000, b"a" * 5000, b"a" * 4999 + b"b"]
    _write_jpgs(tmp_path, payloads)
    # 1 dupe out of 3 = 33% — well over default threshold.
    assert _has_duplicate_thumbnails(str(tmp_path)) is True


# ---------------------------------------------------------------------------
# Integration tests — probe / validator wiring inside generate_images
# ---------------------------------------------------------------------------
#
# These exist because the unit tests above only cover the helpers in
# isolation.  The new branches at the top of generate_images() — and the
# new post-extract retry — need their own coverage so a future refactor
# that breaks the wiring (e.g. drops the validator, flips the did_retry
# bookkeeping) trips the suite.  Boundary mocks only: subprocess.run,
# subprocess.Popen, filesystem.  No autouse helper-patching.


def _make_integration_config(tmp_path):
    """Minimal Config mock matching what generate_images reads."""
    cfg = MagicMock()
    cfg.plex_bif_frame_interval = 2
    cfg.thumbnail_quality = 4
    cfg.tmp_folder = str(tmp_path)
    cfg.ffmpeg_path = "/usr/bin/ffmpeg"
    cfg.ffmpeg_threads = 2
    cfg.tonemap_algorithm = "hable"
    cfg.log_level = "INFO"
    cfg.server_display_name = None
    return cfg


def _wire_integration_mocks(
    *,
    mock_run,
    mock_popen,
    mock_mediainfo,
    mock_exists,
    mock_file,
    mock_detect,
    mock_glob,
    probe_stdout,
    temp_dir,
    duration_ms=None,
):
    """Set up the boundary mocks so generate_images runs end-to-end.

    ``probe_stdout`` is what ffprobe (subprocess.run) returns — the real
    ``_probe_max_keyframe_gap`` parses it just like in production.
    ``subprocess.Popen`` (FFmpeg) reports a clean rc=0 single-call run.

    ``duration_ms`` decides which probe mode runs: ``None`` leaves MediaInfo
    without a usable duration, so the probe falls back to the whole-file
    scan; a real value long enough to sample puts it in sampled mode
    (discussion #283).
    """
    mock_run.return_value = MagicMock(returncode=0, stdout=probe_stdout, stderr="")

    mock_info = MagicMock()
    mock_info.video_tracks = [MagicMock(hdr_format=None, duration=duration_ms)]
    mock_mediainfo.parse.return_value = mock_info

    mock_proc = MagicMock()
    # Two None/0 cycles so the validator-retry test gets a fresh poll
    # sequence for its second FFmpeg pass without StopIteration.
    mock_proc.poll.side_effect = [None, 0, None, 0]
    mock_proc.returncode = 0
    mock_popen.return_value = mock_proc

    mock_exists.return_value = True
    mock_file.return_value.readlines.return_value = []
    mock_detect.return_value = False

    img1 = f"{temp_dir}/img-000001.jpg"
    ts1 = f"{temp_dir}/0000000000.jpg"

    def glob_side_effect(pattern):
        if "img*.jpg" in pattern or "img-*.jpg" in pattern:
            return [img1]
        if pattern.endswith("*.jpg"):
            return [ts1]
        return []

    mock_glob.side_effect = glob_side_effect


@patch("media_preview_generator.processing.generator.MediaInfo")
@patch("subprocess.Popen")
@patch("media_preview_generator.processing.generator.subprocess.run")
@patch("media_preview_generator.processing.generator.os.rename")
@patch("media_preview_generator.processing.generator.os.remove")
@patch("os.path.exists")
@patch("builtins.open", new_callable=mock_open)
@patch("time.sleep")
@patch("media_preview_generator.processing.generator.glob.glob")
@patch("media_preview_generator.processing.generator._detect_codec_error")
@patch("media_preview_generator.processing.generator._has_duplicate_thumbnails")
def test_integration_probe_unsafe_disables_skip_frame(
    mock_has_dupes,
    mock_detect,
    mock_glob,
    mock_sleep,
    mock_file,
    mock_exists,
    mock_remove,
    mock_rename,
    mock_run,
    mock_popen,
    mock_mediainfo,
    tmp_path,
    loguru_caplog,
):
    """Probe returns max_gap > interval → first FFmpeg call drops
    ``-skip_frame:v nokey`` and the WARN log fires once.

    Mirrors the Dhurandhar production case: keyframes at 0 s and 10 s,
    user's interval is 2 s, gap exceeds interval → slow path required.
    """
    # Validator should never run when probe already disabled skip_frame.
    mock_has_dupes.return_value = False
    cfg = _make_integration_config(tmp_path)
    _wire_integration_mocks(
        mock_run=mock_run,
        mock_popen=mock_popen,
        mock_mediainfo=mock_mediainfo,
        mock_exists=mock_exists,
        mock_file=mock_file,
        mock_detect=mock_detect,
        mock_glob=mock_glob,
        probe_stdout="0.000000,K_\n10.000000,K_\n",  # 10s gap > 2s interval
        temp_dir=str(tmp_path),
    )

    with loguru_caplog.at_level("WARNING"):
        generate_images("/test/dhurandhar.mkv", str(tmp_path), None, None, cfg)

    # Exactly one FFmpeg call, and that call had NO -skip_frame flag.
    assert mock_popen.call_count == 1, "Probe-driven slow path must not retry FFmpeg"
    args = mock_popen.call_args_list[0][0][0]
    assert "-skip_frame:v" not in args, (
        "Probe said max_gap=10s > interval=2s — FFmpeg must run without "
        "-skip_frame:v nokey to produce unique thumbnails"
    )
    # User-friendly WARN line fired exactly once.
    warns = [r for r in loguru_caplog.records if r.levelname == "WARNING" and "Slow path" in r.message]
    assert len(warns) == 1, f"Expected one Slow-path WARN, got {len(warns)}"
    assert "snapshot frame every" in warns[0].message
    # The measured gap appears in the message at one-decimal precision
    # (formatted with ``~{:.1f}s``).  Synthetic data was 0.0 → 10.0.
    assert "~10.0s" in warns[0].message
    # Interval is rendered as a plain integer in the message.
    assert "every 2s" in warns[0].message
    # No prescriptive "set to 10s" hint — softer copy keeps it informational.
    assert "Tip:" not in warns[0].message
    # Validator never consulted — we already knew the file was unsafe.
    mock_has_dupes.assert_not_called()


@patch("media_preview_generator.processing.generator.MediaInfo")
@patch("subprocess.Popen")
@patch("media_preview_generator.processing.generator.subprocess.run")
@patch("media_preview_generator.processing.generator.os.rename")
@patch("media_preview_generator.processing.generator.os.remove")
@patch("os.path.exists")
@patch("builtins.open", new_callable=mock_open)
@patch("time.sleep")
@patch("media_preview_generator.processing.generator.glob.glob")
@patch("media_preview_generator.processing.generator._detect_codec_error")
@patch("media_preview_generator.processing.generator._has_duplicate_thumbnails")
def test_integration_probe_inconclusive_disables_skip_frame(
    mock_has_dupes,
    mock_detect,
    mock_glob,
    mock_sleep,
    mock_file,
    mock_exists,
    mock_remove,
    mock_rename,
    mock_run,
    mock_popen,
    mock_mediainfo,
    tmp_path,
    loguru_caplog,
):
    """Probe returns no usable keyframes (corrupt headers, no I-frames)
    → ``_probe_max_keyframe_gap`` returns ``None`` → callers treat it
    as unsafe and disable -skip_frame.  This is the "can't verify the
    file is safe, so take the slow path" branch.
    """
    mock_has_dupes.return_value = False
    cfg = _make_integration_config(tmp_path)
    _wire_integration_mocks(
        mock_run=mock_run,
        mock_popen=mock_popen,
        mock_mediainfo=mock_mediainfo,
        mock_exists=mock_exists,
        mock_file=mock_file,
        mock_detect=mock_detect,
        mock_glob=mock_glob,
        probe_stdout="",  # ffprobe wrote nothing → no keyframes parsed
        temp_dir=str(tmp_path),
    )

    with loguru_caplog.at_level("WARNING"):
        generate_images("/test/unknown.mkv", str(tmp_path), None, None, cfg)

    assert mock_popen.call_count == 1
    args = mock_popen.call_args_list[0][0][0]
    assert "-skip_frame:v" not in args
    warns = [r for r in loguru_caplog.records if r.levelname == "WARNING" and "Slow path" in r.message]
    assert len(warns) == 1
    assert "couldn't read" in warns[0].message.lower()
    mock_has_dupes.assert_not_called()


@patch("media_preview_generator.processing.generator.MediaInfo")
@patch("subprocess.Popen")
@patch("media_preview_generator.processing.generator.subprocess.run")
@patch("media_preview_generator.processing.generator.os.rename")
@patch("media_preview_generator.processing.generator.os.remove")
@patch("os.path.exists")
@patch("builtins.open", new_callable=mock_open)
@patch("time.sleep")
@patch("media_preview_generator.processing.generator.glob.glob")
@patch("media_preview_generator.processing.generator._detect_codec_error")
@patch("media_preview_generator.processing.generator._has_duplicate_thumbnails")
def test_integration_validator_retry_fires_when_fast_path_duplicates(
    mock_has_dupes,
    mock_detect,
    mock_glob,
    mock_sleep,
    mock_file,
    mock_exists,
    mock_remove,
    mock_rename,
    mock_run,
    mock_popen,
    mock_mediainfo,
    tmp_path,
    loguru_caplog,
):
    """Probe clears the fast path, FFmpeg runs once with -skip_frame,
    but the post-extract validator finds duplicate thumbnails → second
    FFmpeg call fires without -skip_frame.  This is the belt-and-braces
    safety net for files whose probe-window looked fine but whose mid-
    file GOP turned out to be sparser than the interval.
    """
    mock_has_dupes.return_value = True  # validator fires the retry
    cfg = _make_integration_config(tmp_path)
    _wire_integration_mocks(
        mock_run=mock_run,
        mock_popen=mock_popen,
        mock_mediainfo=mock_mediainfo,
        mock_exists=mock_exists,
        mock_file=mock_file,
        mock_detect=mock_detect,
        mock_glob=mock_glob,
        # Keyframes 0.5s apart — probe says safe at interval=2.
        probe_stdout="0.000000,K_\n0.500000,K_\n1.000000,K_\n1.500000,K_\n",
        temp_dir=str(tmp_path),
    )

    with loguru_caplog.at_level("WARNING"):
        generate_images("/test/edgecase.mkv", str(tmp_path), None, None, cfg)

    # Exactly two FFmpeg calls: first WITH skip_frame, second WITHOUT.
    assert mock_popen.call_count == 2, "Validator-driven retry must produce exactly two FFmpeg calls"
    first_args = mock_popen.call_args_list[0][0][0]
    second_args = mock_popen.call_args_list[1][0][0]
    assert "-skip_frame:v" in first_args, "First pass should attempt the fast path"
    assert first_args[first_args.index("-skip_frame:v") + 1] == "nokey"
    assert "-skip_frame:v" not in second_args, "Retry must drop -skip_frame to produce unique thumbnails"
    # Validator was consulted exactly once (after first pass; not again after retry).
    mock_has_dupes.assert_called_once()
    # User-friendly WARN explains why we re-ran.
    warns = [r for r in loguru_caplog.records if r.levelname == "WARNING" and "Re-running" in r.message]
    assert len(warns) == 1
    assert "unusual snapshot spacing" in warns[0].message


@patch("media_preview_generator.processing.generator.MediaInfo")
@patch("subprocess.Popen")
@patch("media_preview_generator.processing.generator.subprocess.run")
@patch("media_preview_generator.processing.generator.os.rename")
@patch("media_preview_generator.processing.generator.os.remove")
@patch("os.path.exists")
@patch("builtins.open", new_callable=mock_open)
@patch("time.sleep")
@patch("media_preview_generator.processing.generator.glob.glob")
@patch("media_preview_generator.processing.generator._detect_codec_error")
@patch("media_preview_generator.processing.generator._has_duplicate_thumbnails")
def test_integration_gap_just_within_tolerance_keeps_skip_frame(
    mock_has_dupes,
    mock_detect,
    mock_glob,
    mock_sleep,
    mock_file,
    mock_exists,
    mock_remove,
    mock_rename,
    mock_run,
    mock_popen,
    mock_mediainfo,
    tmp_path,
    loguru_caplog,
):
    """Issue #256 — a keyframe gap a hair beyond the interval but inside the
    6% tolerance keeps the fast path instead of slow-decoding the whole file.

    interval=10s, keyframes at 0 and 10.4s → gap 10.4s.  gap_limit is
    ``10 / 0.94 ≈ 10.64s``, so 10.4 < 10.64 → fast path retained.  This is the
    rounding-error case that previously tripped the strict ``> interval`` check.
    """
    mock_has_dupes.return_value = False  # output is clean, no validator retry
    cfg = _make_integration_config(tmp_path)
    cfg.plex_bif_frame_interval = 10
    _wire_integration_mocks(
        mock_run=mock_run,
        mock_popen=mock_popen,
        mock_mediainfo=mock_mediainfo,
        mock_exists=mock_exists,
        mock_file=mock_file,
        mock_detect=mock_detect,
        mock_glob=mock_glob,
        probe_stdout="0.000000,K_\n10.400000,K_\n",  # 10.4s gap, within tolerance of 10s
        temp_dir=str(tmp_path),
    )

    with loguru_caplog.at_level("WARNING"):
        generate_images("/test/borderline.mkv", str(tmp_path), None, None, cfg)

    # Single fast-path FFmpeg call, WITH -skip_frame, no slow-path warning.
    assert mock_popen.call_count == 1, "Within-tolerance file must not slow-decode"
    args = mock_popen.call_args_list[0][0][0]
    assert "-skip_frame:v" in args, "10.4s gap is within the 6% tolerance of a 10s interval — keep the fast path"
    assert args[args.index("-skip_frame:v") + 1] == "nokey"
    warns = [r for r in loguru_caplog.records if r.levelname == "WARNING" and "Slow path" in r.message]
    assert warns == [], "No slow-path WARN should fire for a within-tolerance gap"
    # The post-extract validator still runs as the safety net.
    mock_has_dupes.assert_called_once()


@patch("media_preview_generator.processing.generator.MediaInfo")
@patch("subprocess.Popen")
@patch("media_preview_generator.processing.generator.subprocess.run")
@patch("media_preview_generator.processing.generator.os.rename")
@patch("media_preview_generator.processing.generator.os.remove")
@patch("os.path.exists")
@patch("builtins.open", new_callable=mock_open)
@patch("time.sleep")
@patch("media_preview_generator.processing.generator.glob.glob")
@patch("media_preview_generator.processing.generator._detect_codec_error")
@patch("media_preview_generator.processing.generator._has_duplicate_thumbnails")
def test_integration_gap_just_beyond_tolerance_disables_skip_frame(
    mock_has_dupes,
    mock_detect,
    mock_glob,
    mock_sleep,
    mock_file,
    mock_exists,
    mock_remove,
    mock_rename,
    mock_run,
    mock_popen,
    mock_mediainfo,
    tmp_path,
    loguru_caplog,
):
    """Issue #256 — a keyframe gap past the 6% tolerance still slow-decodes.

    interval=10s, keyframes at 0 and 10.9s → gap 10.9s > gap_limit ≈ 10.64s,
    so the fast path is disabled (≈8% duplicates would otherwise ship).  This
    is the boundary partner to the within-tolerance test above.
    """
    mock_has_dupes.return_value = False
    cfg = _make_integration_config(tmp_path)
    cfg.plex_bif_frame_interval = 10
    _wire_integration_mocks(
        mock_run=mock_run,
        mock_popen=mock_popen,
        mock_mediainfo=mock_mediainfo,
        mock_exists=mock_exists,
        mock_file=mock_file,
        mock_detect=mock_detect,
        mock_glob=mock_glob,
        probe_stdout="0.000000,K_\n10.900000,K_\n",  # 10.9s gap, beyond tolerance of 10s
        temp_dir=str(tmp_path),
    )

    with loguru_caplog.at_level("WARNING"):
        generate_images("/test/sparse.mkv", str(tmp_path), None, None, cfg)

    assert mock_popen.call_count == 1, "Probe-driven slow path must not retry FFmpeg"
    args = mock_popen.call_args_list[0][0][0]
    assert "-skip_frame:v" not in args, "10.9s gap exceeds the 6% tolerance — must slow-decode"
    warns = [r for r in loguru_caplog.records if r.levelname == "WARNING" and "Slow path" in r.message]
    assert len(warns) == 1
    assert "~10.9s" in warns[0].message
    mock_has_dupes.assert_not_called()


@patch("media_preview_generator.processing.generator.MediaInfo")
@patch("subprocess.Popen")
@patch("media_preview_generator.processing.generator.subprocess.run")
@patch("media_preview_generator.processing.generator.os.rename")
@patch("media_preview_generator.processing.generator.os.remove")
@patch("os.path.exists")
@patch("builtins.open", new_callable=mock_open)
@patch("time.sleep")
@patch("media_preview_generator.processing.generator.glob.glob")
@patch("media_preview_generator.processing.generator._detect_codec_error")
@patch("media_preview_generator.processing.generator._has_duplicate_thumbnails")
def test_integration_known_duration_samples_instead_of_scanning(
    mock_has_dupes,
    mock_detect,
    mock_glob,
    mock_sleep,
    mock_file,
    mock_exists,
    mock_remove,
    mock_rename,
    mock_run,
    mock_popen,
    mock_mediainfo,
    tmp_path,
    loguru_caplog,
):
    """Discussion #283 — when MediaInfo knows the runtime, generate_images
    must hand it to the probe so ffprobe seeks to windows instead of
    reading the whole container.

    The wiring is the point: a regression that stops forwarding duration
    reverts every file to the full scan silently, and the only symptom is
    slowness nobody attributes to this code.  Asserting the flag on the
    ffprobe argv is what catches that.
    """
    mock_has_dupes.return_value = False
    cfg = _make_integration_config(tmp_path)
    _wire_integration_mocks(
        mock_run=mock_run,
        mock_popen=mock_popen,
        mock_mediainfo=mock_mediainfo,
        mock_exists=mock_exists,
        mock_file=mock_file,
        mock_detect=mock_detect,
        mock_glob=mock_glob,
        # Two windows' worth of dense keyframes, ~50 min apart. A probe that
        # counted the between-window jump would report a ~3000s gap and
        # slow-decode this file.
        probe_stdout="600.000000,K_\n601.000000,K_\n3600.000000,K_\n3601.000000,K_\n",
        temp_dir=str(tmp_path),
        duration_ms=7200_000,
    )

    with loguru_caplog.at_level("WARNING"):
        generate_images("/test/long_movie.mkv", str(tmp_path), None, None, cfg)

    probe_argv = mock_run.call_args[0][0]
    assert "-read_intervals" in probe_argv, "known duration must produce a sampled probe, not a full scan"
    intervals = probe_argv[probe_argv.index("-read_intervals") + 1].split(",")
    assert len(intervals) == KEYFRAME_PROBE_WINDOWS
    # Pin *which* value was forwarded, not merely that sampling happened:
    # passing the raw interval instead of the #256-tolerant gap_limit would
    # leave the windows 6% short and every assertion above still green.
    expected_window = _keyframe_probe_window_length(cfg.plex_bif_frame_interval / (1 - KEYFRAME_DUPLICATE_TOLERANCE))
    assert float(intervals[0].split("%+")[1]) == pytest.approx(round(expected_window), abs=0.5)

    assert mock_popen.call_count == 1
    args = mock_popen.call_args_list[0][0][0]
    assert "-skip_frame:v" in args, "1s keyframes at a 2s interval — the fast path must survive sampling"
    warns = [r for r in loguru_caplog.records if r.levelname == "WARNING" and "Slow path" in r.message]
    assert warns == []
    mock_has_dupes.assert_called_once()
