"""Tests for the stale-frame bug: frames left by a previous run must never
be counted as if this run had produced them.

The frame-cache slot for a file is deterministic (``frames-<sha256[:16]>``),
so a re-extraction lands in the *same* directory as the previous one. The
rename step maps FFmpeg's ``img-%06d.jpg`` onto ``{frame_no * interval}.jpg``,
so a run at a different thumbnail interval writes a *different* set of
filenames (2 s → 0, 2, 4 …; 5 s → 0, 5, 10 …). Only the colliding names get
overwritten; the rest survive and were being counted into the BIF.

Production case: a 1 h 55 m film first generated at a 2 s interval (3451
frames) and later re-generated at 5 s (1381 frames) left 4141 JPGs on disk —
a BIF spanning 5 h 45 m for a 1 h 55 m movie, so Plex's scrubber drifted ~3x.

Only the FFmpeg boundary is mocked; the rename/count/cleanup logic runs for
real against ``tmp_path`` so the regression is actually exercised.
"""

import logging
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger

from media_preview_generator.processing.generator import generate_images


@pytest.fixture
def loguru_caplog(caplog):
    """Bridge loguru → pytest's caplog so we can assert on the user-facing
    WARN. Mirrors the helper in ``test_processing_keyframe_probe.py``.
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


def _ffmpeg_writing(n_frames: int, output_folder: str):
    """Build a ``create_ffmpeg_runner`` stand-in whose runner writes
    ``n_frames`` real ``img-%06d.jpg`` files, exactly as FFmpeg would.
    """

    def factory(**_kwargs):
        def _run(use_skip=False, init_vulkan=False, **_):
            for i in range(1, n_frames + 1):
                with open(os.path.join(output_folder, f"img-{i:06d}.jpg"), "wb") as fh:
                    fh.write(b"\xff\xd8\xff\xdbFRESH")
            return 0, 1.0, "10x", []

        return _run

    return factory


def _seed_previous_run(output_folder: str, interval: int, duration: float) -> int:
    """Write the frames a previous run at ``interval`` would have left behind
    (post-rename names: ``{frame_no * interval:010d}.jpg``). Returns the count.
    """
    n = int(duration // interval) + 1
    for k in range(n):
        with open(os.path.join(output_folder, f"{k * interval:010d}.jpg"), "wb") as fh:
            fh.write(b"\xff\xd8\xff\xdbSTALE")
    return n


@pytest.fixture
def sdr_config(mock_config):
    """mock_config, pinned to the 5 s interval the production case used."""
    mock_config.plex_bif_frame_interval = 5
    return mock_config


@patch("media_preview_generator.processing.generator._probe_max_keyframe_gap", return_value=None)
@patch("media_preview_generator.processing.generator.MediaInfo")
def test_generate_images_ignores_frames_left_by_a_previous_run(mock_mediainfo, _mock_probe, sdr_config, tmp_path):
    """A previous run at a *different* interval must not inflate this run's
    frame count (the Annihilation 4141-vs-1381 regression).
    """
    mock_mediainfo.parse.return_value = MagicMock(video_tracks=[])
    out = str(tmp_path)

    duration = 6900.43  # 1 h 55 m 00 s — the production film
    stale = _seed_previous_run(out, interval=2, duration=duration)
    fresh = int(duration // 5) + 1  # what a 5 s run legitimately produces

    with patch(
        "media_preview_generator.processing.ffmpeg_runner.create_ffmpeg_runner",
        _ffmpeg_writing(fresh, out),
    ):
        success, image_count, *_ = generate_images(str(tmp_path / "movie.mkv"), out, None, None, sdr_config)

    assert success is True
    # Before the fix this returned 4141 (the union of the 2 s and 5 s grids).
    assert image_count == fresh, f"stale frames from the previous {stale}-frame run were counted"
    assert len(list(tmp_path.glob("*.jpg"))) == fresh


@patch("media_preview_generator.processing.generator._probe_max_keyframe_gap", return_value=None)
@patch("media_preview_generator.processing.generator.MediaInfo")
def test_generate_images_frame_count_matches_movie_length(mock_mediainfo, _mock_probe, sdr_config, tmp_path):
    """The BIF's implied length (frames x interval) must match the movie, so
    Plex's scrubber stays in sync.
    """
    mock_mediainfo.parse.return_value = MagicMock(video_tracks=[])
    out = str(tmp_path)

    duration = 6900.43
    _seed_previous_run(out, interval=2, duration=duration)
    fresh = int(duration // 5) + 1

    with patch(
        "media_preview_generator.processing.ffmpeg_runner.create_ffmpeg_runner",
        _ffmpeg_writing(fresh, out),
    ):
        _, image_count, *_ = generate_images(str(tmp_path / "movie.mkv"), out, None, None, sdr_config)

    bif_seconds = image_count * sdr_config.plex_bif_frame_interval
    assert abs(bif_seconds - duration) < sdr_config.plex_bif_frame_interval * 2, (
        f"BIF spans {bif_seconds}s for a {duration}s movie"
    )


@patch("media_preview_generator.processing.generator._probe_max_keyframe_gap", return_value=None)
@patch("media_preview_generator.processing.generator.MediaInfo")
def test_generate_images_drops_tail_frames_when_source_gets_shorter(mock_mediainfo, _mock_probe, sdr_config, tmp_path):
    """Same interval, but the source was replaced by a shorter cut.

    The grids align, so nothing collides at the tail: the previous (longer)
    run's high-numbered frames would survive and pad the BIF past the end of
    the new, shorter movie. A distinct mechanism from the interval-change
    case, with the same symptom.
    """
    mock_mediainfo.parse.return_value = MagicMock(video_tracks=[])
    out = str(tmp_path)

    _seed_previous_run(out, interval=5, duration=6900.0)  # the old, longer cut
    fresh = int(3600.0 // 5) + 1  # re-extract of a 1 h cut

    with patch(
        "media_preview_generator.processing.ffmpeg_runner.create_ffmpeg_runner",
        _ffmpeg_writing(fresh, out),
    ):
        _, image_count, *_ = generate_images(str(tmp_path / "movie.mkv"), out, None, None, sdr_config)

    assert image_count == fresh, "tail frames from the previous longer cut survived"


@patch("media_preview_generator.processing.generator._probe_max_keyframe_gap", return_value=None)
@patch("media_preview_generator.processing.generator.MediaInfo")
def test_generate_images_warns_when_thumbnails_do_not_span_the_movie(
    mock_mediainfo, _mock_probe, sdr_config, tmp_path, loguru_caplog
):
    """Backstop: a BIF whose length disagrees with the runtime must be loud.

    Drives the guard directly by having FFmpeg under-produce, which stands in
    for any future route to a wrong count (partial unpack, truncated run).
    """
    track = MagicMock()
    track.duration = 6900430  # ms — a 1 h 55 m film
    mock_mediainfo.parse.return_value = MagicMock(video_tracks=[track])
    out = str(tmp_path)

    with patch(
        "media_preview_generator.processing.ffmpeg_runner.create_ffmpeg_runner",
        _ffmpeg_writing(50, out),  # nowhere near the ~1381 a 5 s interval needs
    ):
        _, image_count, *_ = generate_images(str(tmp_path / "movie.mkv"), out, None, None, sdr_config)

    assert image_count == 50
    assert "out of sync" in loguru_caplog.text
    assert "115 min" in loguru_caplog.text  # the real runtime is surfaced


@patch("media_preview_generator.processing.generator._probe_max_keyframe_gap", return_value=None)
@patch("media_preview_generator.processing.generator.MediaInfo")
def test_generate_images_stays_quiet_when_thumbnails_span_the_movie(
    mock_mediainfo, _mock_probe, sdr_config, tmp_path, loguru_caplog
):
    """The guard must not cry wolf on a healthy extraction."""
    track = MagicMock()
    track.duration = 6900430
    mock_mediainfo.parse.return_value = MagicMock(video_tracks=[track])
    out = str(tmp_path)

    fresh = int(6900.43 // 5) + 1
    with patch(
        "media_preview_generator.processing.ffmpeg_runner.create_ffmpeg_runner",
        _ffmpeg_writing(fresh, out),
    ):
        generate_images(str(tmp_path / "movie.mkv"), out, None, None, sdr_config)

    assert "out of sync" not in loguru_caplog.text


# ---------------------------------------------------------------------------
# The clean above empties the shared frame-cache slot, so a regenerate run
# must hold the per-path generation lock while it does — otherwise a
# concurrent non-regenerate dispatch could be cache-HITting that same
# directory and reading the JPGs we are deleting.
# ---------------------------------------------------------------------------


def _emby_registry(tmp_path: Path):
    """Minimal single-server registry whose library covers ``tmp_path/data/movies``."""
    from media_preview_generator.servers import ServerRegistry

    return ServerRegistry.from_settings(
        [
            {
                "id": "emby-1",
                "type": "emby",
                "name": "Emby",
                "enabled": True,
                "url": "http://emby:8096",
                "auth": {"method": "api_key", "api_key": "k"},
                "libraries": [
                    {
                        "id": "1",
                        "name": "Movies",
                        "remote_paths": [str(tmp_path / "data" / "movies")],
                        "enabled": True,
                    }
                ],
                "output": {"adapter": "emby_sidecar", "width": 320, "frame_interval": 10},
            }
        ],
    )


@pytest.mark.parametrize("regenerate", [True, False])
def test_shared_frame_slot_is_always_under_the_generation_lock(mock_config, tmp_path, regenerate):
    """Both regenerate and non-regenerate dispatches write to the shared
    ``frames-<hash>/`` slot, so both must hold the per-path lock.

    Regenerate is the one that matters: it *empties* the slot before
    extracting, so without the lock a concurrent cache-HIT reader would have
    the JPGs deleted out from under it mid-publish.
    """
    from media_preview_generator.processing import multi_server as ms

    media = tmp_path / "data" / "movies" / "Test (2024)" / "Test (2024).mkv"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(b"placeholder")

    mock_config.working_tmp_folder = str(tmp_path / "tmp")
    mock_config.tmp_folder = str(tmp_path / "tmp")

    lock = MagicMock()
    real_cache = ms.get_frame_cache(base_dir=tmp_path / "frame_cache")
    spy = MagicMock(wraps=real_cache)
    spy.generation_lock.return_value = lock
    spy.frame_dir_for.side_effect = real_cache.frame_dir_for

    def fake_generate_images(video_file, output_folder, *args, **kwargs):
        from PIL import Image

        Path(output_folder).mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", (320, 180), (10, 20, 30))
        for i in range(5):
            img.save(Path(output_folder) / f"{i:05d}.jpg", "JPEG", quality=70)
        return (True, 5, "h264", 1.0, 30.0, None)

    with (
        patch.object(ms, "get_frame_cache", return_value=spy),
        patch.object(ms, "generate_images", side_effect=fake_generate_images),
    ):
        ms.process_canonical_path(
            canonical_path=str(media),
            registry=_emby_registry(tmp_path),
            config=mock_config,
            regenerate=regenerate,
        )

    assert lock.acquire.called, "the shared frame slot was touched without taking the generation lock"
    assert lock.release.called, "the generation lock was not released"
    if regenerate:
        # Regenerate must re-extract, never take a cache hit.
        assert not spy.get.called, "regenerate must not read the cache"
