"""Real ffmpeg: a generated 60 s stereo tone file fingerprints to ~window/POINT_S points."""

import shutil
import subprocess

import pytest

from media_preview_generator.markers.audio import POINT_S
from media_preview_generator.markers.audio.fingerprint import chromaprint_ffmpeg, compute_fingerprint, window_s

pytestmark = pytest.mark.integration


def test_real_ffmpeg_fingerprint_length(tmp_path):
    ffmpeg = chromaprint_ffmpeg(shutil.which("ffmpeg"))
    if ffmpeg is None:
        pytest.skip("no ffmpeg with chromaprint")
    media = tmp_path / "tone.mka"
    subprocess.run(
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "anoisesrc=color=pink:seed=7:d=60", "-ac", "2", str(media)],
        check=True,
    )
    points = compute_fingerprint(str(media), 60_000, ffmpeg=ffmpeg)
    # Chromaprint emits nothing until its FFT frame and filter context are full (~2.5 s), so a window yields a
    # near-constant ~20 points fewer than window/POINT_S (21 short for both 21 s and 42 s windows on ffmpeg 8.0.1).
    expected = window_s(60_000) / POINT_S
    assert expected - 30 < len(points) <= expected
