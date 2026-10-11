"""Real FFmpeg color-range and empty-output checks on tiny generated media."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from PIL import Image, ImageChops, ImageStat

from media_preview_generator.config import _resolve_ffmpeg_path
from media_preview_generator.processing import chapters

pytestmark = [pytest.mark.integration, pytest.mark.timeout(30)]


@pytest.fixture
def hdr_movie(tmp_path, request):
    ffmpeg = _resolve_ffmpeg_path()
    if not ffmpeg:
        pytest.skip("Real FFmpeg is required")
    transfer = request.param
    source = tmp_path / "synthetic.mkv"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=6",
            "-t",
            "2",
            "-vf",
            "format=yuv420p10le",
            "-c:v",
            "libx265",
            "-x265-params",
            f"pools=1:frame-threads=1:colorprim=bt2020:transfer={transfer}:colormatrix=bt2020nc:range=limited",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-color_primaries",
            "bt2020",
            "-color_trc",
            transfer,
            "-colorspace",
            "bt2020nc",
            "-color_range",
            "tv",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=60,  # x265 is slow on a shared CI runner while other workers decode
    )
    config = SimpleNamespace(
        ffmpeg_path=ffmpeg, ffmpeg_threads=2, thumbnail_quality=4, tonemap_algorithm="hable", log_level="INFO"
    )
    return source, config, transfer


@pytest.mark.parametrize("hdr_movie", ["smpte2084", "arib-std-b67"], indirect=True)
def test_hdr_chapter_matches_independent_full_range_reference(hdr_movie, tmp_path):
    source, config, transfer = hdr_movie
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    actual = tmp_path / "chapter.jpg"
    chapters.extract_chapter_frame(str(source), 1000, actual, config)
    reference = tmp_path / "reference.jpg"
    # Convert in zscale before resizing, independently of the app's swscale range conversion.
    filters = (
        f"zscale=tin={transfer}:pin=bt2020:min=bt2020nc:t=linear:npl=100,format=gbrpf32le,"
        "zscale=p=bt709,tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=full,"
        "format=yuvj420p,scale=w=1280:h=-2"
    )
    subprocess.run(
        [
            config.ffmpeg_path,
            "-v",
            "error",
            "-y",
            "-ss",
            "1",
            "-i",
            str(source),
            "-an",
            "-sn",
            "-dn",
            "-vf",
            filters,
            "-frames:v",
            "1",
            "-threads",
            "2",
            "-filter_threads",
            "2",
            "-q:v",
            "4",
            str(reference),
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    difference = ImageChops.difference(Image.open(actual).convert("RGB"), Image.open(reference).convert("RGB"))
    assert max(ImageStat.Stat(difference).mean) < 3
    assert Image.open(actual).size == (1280, 720)
    probe = subprocess.run(
        [shutil.which("ffprobe"), "-v", "error", "-show_streams", "-of", "json", str(actual)],
        check=True,
        capture_output=True,
        timeout=5,
        text=True,
    )
    assert json.loads(probe.stdout)["streams"][0]["color_range"] == "pc"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original


@pytest.mark.parametrize("hdr_movie", ["smpte2084", "arib-std-b67"], indirect=True)
def test_hdr_chapter_beyond_video_ends_with_specific_metadata_error(hdr_movie, tmp_path):
    source, config, _ = hdr_movie
    original = source.read_bytes()
    output = tmp_path / "outside.jpg"
    with pytest.raises(ValueError, match="outside the current video's duration"):
        chapters.extract_chapter_frame(str(source), 75000, output, config)
    assert not output.exists()
    assert source.read_bytes() == original


@pytest.mark.parametrize("hdr_movie", ["smpte2084"], indirect=True)
@pytest.mark.parametrize(
    ("general_duration_known", "start_ms", "message"),
    [
        (True, 75000, "outside the current video's duration"),
        (False, 75000, "No video frame was available"),
        (True, 2500, "outside the current video's duration"),
    ],
)
def test_missing_video_duration_never_turns_empty_output_into_missing_source_retry(
    hdr_movie, tmp_path, monkeypatch, general_duration_known, start_ms, message
):
    source, config, _ = hdr_movie
    media = chapters.MediaInfo.parse(str(source))
    assert float(media.general_tracks[0].duration) == 2000
    # Reproduce MediaInfo's container-only duration shape seen in production.
    media.video_tracks[0].duration = None
    if not general_duration_known:
        media.general_tracks[0].duration = None
    factory = chapters.create_ffmpeg_runner
    starts = []

    def record_real_attempt(**options):
        starts.append(options["chapter_start_ms"])
        return factory(**options)

    monkeypatch.setattr(chapters, "create_ffmpeg_runner", record_real_attempt)
    output = tmp_path / "outside.jpg"
    original = source.read_bytes()
    with pytest.raises(ValueError, match=message):
        chapters.extract_chapter_frame(str(source), start_ms, output, config, media_info=media)
    assert starts == [start_ms]
    assert not output.exists()
    assert source.read_bytes() == original
