"""Real ffmpeg: the end-picture check lines up two episodes' pictures from their fingerprints' alignment, in mkv and
mpegts, with the audio starting with the picture or 2 s late, on the CPU and (when present) CUDA; and every vendor's
decode gives the CPU's frames byte for byte.

Each synthetic episode shows a different picture every second over random-note audio; the partners carry the same
content 5 s later, some with the audio shifted late in the container. The check must find every picture alike at the
aligned offset and none a second either way, so a change to the fingerprint command (``-copyts``, ``apad``, a seek) or
to how the audio start is read can't silently move the offsets.
"""

from __future__ import annotations

import shutil
import subprocess
import wave

import numpy as np
import pytest

from media_preview_generator.markers.audio import end_picture as ep
from media_preview_generator.markers.audio.fingerprint import chromaprint_ffmpeg, compute_fingerprint
from media_preview_generator.markers.audio.season import season_pair_runs
from media_preview_generator.markers.credits import frames
from media_preview_generator.markers.probe import ffprobe_path_for, stream_starts
from tests.markers.credits.test_frames_integration import vaapi_node

# The module's clips are encoded on the CPU (libx264 and libx265 at 1280x720) in fixture setup, which pytest-timeout
# counts: 25-40 s under a full xdist run, past the suite's 30 s.
pytestmark = [pytest.mark.integration, pytest.mark.timeout(180)]
W, H, FPS, RATE = 320, 180, 10, 44100
SECONDS = 45
PREFIX_S = 5
# Passed as the duration so the fingerprint window (35 %) covers the shared stretch.
DURATION_MS = 120_000


def _pictures(rng: np.random.Generator, count: int) -> list[np.ndarray]:
    return [rng.integers(0, 256, (H // 4, W // 4), dtype=np.uint8).repeat(4, 0).repeat(4, 1) for _ in range(count)]


def _notes(seconds: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(RATE // 4) / RATE
    tones = [110 * 2 ** (rng.integers(0, 48) / 12) for _ in range(seconds * 4)]
    return np.concatenate([sum(np.sin(2 * np.pi * f * k * t) / k for k in (1, 2, 3)) * 0.2 for f in tones])


def _wav(samples: np.ndarray, path) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())


@pytest.fixture(scope="module")
def season_files(tmp_path_factory):
    ffmpeg = chromaprint_ffmpeg(shutil.which("ffmpeg"))
    if ffmpeg is None:
        pytest.skip("no ffmpeg with chromaprint")
    root = tmp_path_factory.mktemp("end-picture")
    rng = np.random.default_rng(0)
    content, prefix = _pictures(rng, SECONDS), _pictures(rng, PREFIX_S)

    def video(pictures, name):
        raw = b"".join(picture.tobytes() * FPS for picture in pictures)
        subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{W}x{H}", "-r",
                        str(FPS), "-i", "-", "-c:v", "libx264", "-g", str(2 * FPS), "-pix_fmt", "yuv420p",
                        str(root / name)], input=raw, check=True)  # fmt: skip

    def mux(video_name, audio_name, name, *extra):
        subprocess.run([ffmpeg, "-y", "-v", "error", "-i", str(root / video_name), *extra, "-i", str(root / audio_name),
                        "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", str(root / name)], check=True)  # fmt: skip

    audio, lead_in = _notes(SECONDS, 1), _notes(PREFIX_S, 2)
    video(content, "vA.mkv")
    video(prefix + content, "vB.mkv")
    _wav(audio, root / "aA.wav")
    _wav(np.concatenate([lead_in, audio]), root / "aB.wav")
    _wav(np.concatenate([lead_in, audio])[2 * RATE :], root / "aC.wav")
    mux("vA.mkv", "aA.wav", "A.mkv")
    mux("vB.mkv", "aB.wav", "B.mkv")
    mux("vB.mkv", "aC.wav", "C.mkv", "-itsoffset", "2")  # its audio starts 2 s into the file
    for source, name in (("B.mkv", "D.ts"), ("C.mkv", "E.ts")):
        subprocess.run([ffmpeg, "-y", "-v", "error", "-i", str(root / source), "-c", "copy", str(root / name)],
                       check=True)  # fmt: skip
    return ffmpeg, {name: str(root / name) for name in ("A.mkv", "B.mkv", "C.mkv", "D.ts", "E.ts")}


def _decoders():
    yield pytest.param(None, None, id="cpu")
    if shutil.which("nvidia-smi") and subprocess.run(["nvidia-smi", "-L"], capture_output=True).returncode == 0:
        yield pytest.param("NVIDIA", "cuda:0", id="cuda")


@pytest.mark.parametrize(("gpu", "device"), list(_decoders()))
@pytest.mark.parametrize(
    ("partner", "audio_late"),
    [("B.mkv", False), ("C.mkv", True), ("D.ts", False), ("E.ts", True)],
    ids=["mkv", "mkv-late-audio", "mpegts", "mpegts-late-audio"],
)
def test_the_aligned_pictures_match_and_a_second_either_way_do_not(season_files, partner, audio_late, gpu, device):
    ffmpeg, paths = season_files
    points = {name: compute_fingerprint(paths[name], DURATION_MS, ffmpeg=ffmpeg) for name in ("A.mkv", partner)}
    run = max(season_pair_runs(points["A.mkv"], points[partner]), key=lambda r: r.a_end_s - r.a_start_s)
    offset_s = run.b_start_s - run.a_start_s
    starts = stream_starts(paths[partner], ffprobe=ffprobe_path_for(ffmpeg))
    # The audio shifted 2 s late moves the fingerprints' alignment by 2 s; only its start time puts it back.
    assert (starts.audio_offset_s > 1.5) is audio_late
    assert abs(offset_s + starts.audio_offset_s - PREFIX_S) < 0.2
    reader = ep.Reader(ffmpeg=ffmpeg, gpu=gpu, gpu_device_path=device)
    end_s = min(run.a_end_s, 30.0)
    shares = [reader.share(paths["A.mkv"], paths[partner], run.a_start_s, end_s, offset_s + d) for d in (0, -1, 1)]
    assert shares == [1.0, 0.0, 0.0]


@pytest.fixture(scope="module")
def detailed_clips(tmp_path_factory):
    """12 s of fine moving detail at 1280x720, as 8-bit H.264 and (with an HEVC encoder) 10-bit HEVC: detail each
    vendor's own scaler blurred differently (scale_cuda, scale_vaapi, swscale's bicubic)."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("no ffmpeg")
    root = tmp_path_factory.mktemp("end-picture-vendors")
    source = "mandelbrot=size=1280x720:rate=24,trim=duration=12,setpts=PTS-STARTPTS"
    encodes = {"8-bit": ["-c:v", "libx264", "-g", "48", "-pix_fmt", "yuv420p"]}
    if "libx265" in subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout:
        encodes["10-bit"] = ["-c:v", "libx265", "-x265-params", "keyint=48:log-level=error", "-pix_fmt", "yuv420p10le"]
    for name, codec in encodes.items():
        subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", source, *codec, str(root / f"{name}.mkv")],
                       check=True)  # fmt: skip
    return ffmpeg, {name: str(root / f"{name}.mkv") for name in encodes}


def _end_picture_frames(monkeypatch, ffmpeg, path, gpu, device):
    """The check's decode of 8.2-11.7 s (the reader's window for a stretch ending at 11.2 s): each frame's time, its
    320x180 luma plane as ffmpeg wrote it, and whether each ffmpeg run was on the GPU."""
    planes, runs = [], []
    shrink, run_decode = ep._shrink, frames.run_decode

    def keep_planes(chunk):
        planes.extend(plane.tobytes() for plane in chunk)
        return shrink(chunk)

    def note_run(command, *, hw_active, **kwargs):
        runs.append(hw_active)
        return run_decode(command, hw_active=hw_active, **kwargs)

    monkeypatch.setattr(ep, "_shrink", keep_planes)
    monkeypatch.setattr(frames, "run_decode", note_run)
    starts = stream_starts(path, ffprobe=ffprobe_path_for(ffmpeg))
    got = ep.decode_frames(path, 8.2, 3.5, ffmpeg=ffmpeg, gpu=gpu, gpu_device_path=device,
                           container_start_s=starts.container_s,
                           download_format=frames.DOWNLOAD_FORMATS.get(starts.pix_fmt or ""))  # fmt: skip
    monkeypatch.undo()
    return [t for t, _ in got], planes, runs


def _same_frames_as_the_cpu(monkeypatch, detailed_clips, clip_name, gpu, device):
    ffmpeg, paths = detailed_clips
    if clip_name not in paths:
        pytest.skip("no HEVC encoder")
    starts = stream_starts(paths[clip_name], ffprobe=ffprobe_path_for(ffmpeg))
    # Downloaded as surfaces in the stream's own format, so this is the hwdownload path, not ffmpeg's own download.
    assert frames.DOWNLOAD_FORMATS[starts.pix_fmt] == {"8-bit": "nv12", "10-bit": "p010le"}[clip_name]
    cpu_times, cpu_planes, cpu_runs = _end_picture_frames(monkeypatch, ffmpeg, paths[clip_name], None, None)
    gpu_times, gpu_planes, gpu_runs = _end_picture_frames(monkeypatch, ffmpeg, paths[clip_name], gpu, device)
    assert (cpu_runs, gpu_runs) == ([False], [True])  # one ffmpeg each: the GPU decode didn't fall back to the CPU
    assert len(cpu_planes) == 7 and cpu_times == gpu_times
    assert gpu_planes == cpu_planes


@pytest.mark.gpu
@pytest.mark.parametrize("clip_name", ["8-bit", "10-bit"])
def test_cuda_gives_the_cpus_end_picture_frames_byte_for_byte(monkeypatch, detailed_clips, clip_name):
    # Partners of one season can be decoded by workers of different vendors: only the same frames on every one keep
    # the answer from depending on which worker's GPU read which file (scale_cuda's frames weren't the CPU's).
    if shutil.which("nvidia-smi") is None:
        pytest.skip("no NVIDIA GPU")
    _same_frames_as_the_cpu(monkeypatch, detailed_clips, clip_name, "NVIDIA", "cuda:0")


@pytest.mark.gpu
@pytest.mark.parametrize("clip_name", ["8-bit", "10-bit"])
def test_vaapi_gives_the_cpus_end_picture_frames_byte_for_byte(monkeypatch, detailed_clips, clip_name):
    # Intel and AMD only: storage has an NVIDIA render node, so this skips there and runs in the lab image.
    node = vaapi_node()
    if node is None:
        pytest.skip("no Intel or AMD render node")
    device, vendor = node
    _same_frames_as_the_cpu(monkeypatch, detailed_clips, clip_name, vendor, device)
