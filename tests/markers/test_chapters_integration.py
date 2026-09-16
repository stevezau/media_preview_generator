"""Real ffmpeg/ffprobe: a generated file with named chapters decodes to the right candidates."""

import shutil
import subprocess

import pytest

from media_preview_generator.markers.models import MarkerType
from media_preview_generator.markers.probe import probe_media
from media_preview_generator.markers.sources.chapters import chapter_candidates

pytestmark = pytest.mark.integration


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="needs ffmpeg")
def test_generated_mkv_with_chapters(tmp_path):
    meta = tmp_path / "meta.txt"
    meta.write_text(
        ";FFMETADATA1\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=5000\ntitle=Previously\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=5000\nEND=20000\ntitle=Opening Credits\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=20000\nEND=50000\ntitle=Chapter 2\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=50000\nEND=60000\ntitle=End Credits\n"
    )
    out = tmp_path / "x.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x36:d=60",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-i",
            str(meta),
            "-map_metadata",
            "2",
            "-map_chapters",
            "2",
            "-shortest",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(out),
        ],
        check=True,
    )
    probe = probe_media(str(out), ffprobe="ffprobe")
    assert abs(probe.duration_ms - 60_000) < 200
    types = [(c.type, c.start_ms, c.end_ms) for c in chapter_candidates(probe)]
    assert types == [(MarkerType.RECAP, 0, 5000), (MarkerType.INTRO, 5000, 20000), (MarkerType.CREDITS, 50000, 60000)]


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="needs ffmpeg")
def test_generated_mkv_generic_intro_dropped_next_to_op(tmp_path):
    """End to end: a generic "Intro" chapter next to "OP" (the cold open) must not publish
    (Mushoku Tensei S01E06/07/08, JJK S02E08/E13 real-library evidence)."""
    meta = tmp_path / "meta.txt"
    meta.write_text(
        ";FFMETADATA1\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=20000\ntitle=Intro\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=20000\nEND=35000\ntitle=OP\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=35000\nEND=60000\ntitle=Chapter 2\n"
    )
    out = tmp_path / "y.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x36:d=60",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-i",
            str(meta),
            "-map_metadata",
            "2",
            "-map_chapters",
            "2",
            "-shortest",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(out),
        ],
        check=True,
    )
    probe = probe_media(str(out), ffprobe="ffprobe")
    types = [(c.type, c.start_ms, c.end_ms, c.origin) for c in chapter_candidates(probe)]
    assert types == [(MarkerType.INTRO, 20000, 35000, "OP")]
