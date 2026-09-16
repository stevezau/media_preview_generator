"""Real ffmpeg on a generated clip: keyframe rows of the tail and 1 fps rows, on the CPU and (when present) CUDA."""

import glob
import pathlib
import shutil
import subprocess

import pytest

from media_preview_generator.markers.credits import frames

pytestmark = pytest.mark.integration
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
VAAPI_DRIVERS = {"i915": "INTEL", "xe": "INTEL", "amdgpu": "AMD", "radeon": "AMD"}


def vaapi_node() -> tuple[str, str] | None:
    """A render node this decode can use, with its GPU type (NVIDIA's node has no VAAPI decode)."""
    for node in sorted(glob.glob("/dev/dri/renderD*")):
        driver = pathlib.Path(f"/sys/class/drm/{pathlib.Path(node).name}/device/driver")
        gpu = VAAPI_DRIVERS.get(driver.resolve().name) if driver.exists() else None
        if gpu:
            return node, gpu
    return None


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("no ffmpeg")
    if not pathlib.Path(FONT).is_file():
        pytest.skip("no DejaVuSans font for the credit roll")
    path = tmp_path_factory.mktemp("credits") / "clip.mkv"
    # 0–60 s bright moving pattern without text, 60–90 s white names on black; a keyframe every 2 s.
    story = "mandelbrot=size=640x360:rate=24,trim=duration=60,setpts=PTS-STARTPTS"
    roll = f"color=c=black:size=640x360:rate=24:duration=30,drawtext=fontfile={FONT}:text='DIRECTED BY A NAME':fontcolor=white:fontsize=28:x=(w-tw)/2:y=h-40*t"
    subprocess.run(
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i", story, "-f", "lavfi", "-i", roll, "-filter_complex",
         "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-c:v", "libx264", "-g", "48", "-pix_fmt", "yuv420p",
         str(path)],
        check=True,
    )  # fmt: skip
    return ffmpeg, str(path)


@pytest.fixture(scope="module")
def recording(tmp_path_factory):
    """A recorded-TV style .ts whose timestamps start 30000 s in, like a DVB capture's PCR base."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("no ffmpeg")
    path = tmp_path_factory.mktemp("credits-ts") / "recording.ts"
    subprocess.run(
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10:duration=40", "-c:v", "libx264",
         "-g", "20", "-pix_fmt", "yuv420p", "-output_ts_offset", "30000", "-muxdelay", "0", "-muxpreload", "0",
         str(path)],
        check=True,
    )  # fmt: skip
    return ffmpeg, str(path)


def _rows(clip, **kwargs):
    ffmpeg, path = clip
    return frames.decode_rows(path, ffmpeg=ffmpeg, count_boxes=lambda planes: [0] * len(planes), **kwargs)


def test_cpu_keyframes_of_the_tail(clip):
    rows = _rows(clip, start_s=30.0, length_s=None, keyframes_only=True, fps=None, gpu=None, gpu_device_path=None)
    pts = [r[0] for r in rows]
    assert 25 <= len(rows) <= 32 and pts == sorted(pts) and 28.0 <= pts[0] <= 32.0
    assert all(r[2] < 30 for r in rows if r[0] >= 61)  # the roll is dark


def test_cpu_one_frame_a_second_before_a_time(clip):
    rows = _rows(clip, start_s=50.0, length_s=21.0, keyframes_only=False, fps=1, gpu=None, gpu_device_path=None)
    assert [round(r[0]) for r in rows] == list(range(50, 71))


def test_a_recording_with_a_pcr_base_reports_file_seconds(recording):
    # Without subtracting the container's start_time every row here reads 30020, 30021 …: the tail window, rule J and
    # the published marker would all be tens of thousands of seconds out.
    refine = _rows(recording, start_s=20.0, length_s=11.0, keyframes_only=False, fps=1, gpu=None, gpu_device_path=None)
    assert [round(r[0]) for r in refine] == list(range(20, 31))
    keyframes = _rows(
        recording, start_s=20.0, length_s=None, keyframes_only=True, fps=None, gpu=None, gpu_device_path=None
    )
    pts = [r[0] for r in keyframes]
    assert pts and pts[0] == 20.0 and pts == sorted(pts) and pts[-1] < 40.0


@pytest.mark.gpu
def test_vaapi_gives_the_same_timestamps(clip):
    # Intel and AMD only: storage has an NVIDIA render node, so this skips there and runs in the lab image.
    node = vaapi_node()
    if node is None:
        pytest.skip("no Intel or AMD render node")
    device, gpu = node
    cpu = _rows(clip, start_s=30.0, length_s=None, keyframes_only=True, fps=None, gpu=None, gpu_device_path=None)
    vaapi = _rows(clip, start_s=30.0, length_s=None, keyframes_only=True, fps=None, gpu=gpu, gpu_device_path=device)
    assert [r[0] for r in vaapi] == [r[0] for r in cpu]
    assert all(abs(a[2] - b[2]) < 3 for a, b in zip(cpu, vaapi, strict=True))


@pytest.mark.gpu
def test_cuda_gives_the_same_timestamps(clip):
    if shutil.which("nvidia-smi") is None:
        pytest.skip("no NVIDIA GPU")
    cpu = _rows(clip, start_s=30.0, length_s=None, keyframes_only=True, fps=None, gpu=None, gpu_device_path=None)
    gpu = _rows(
        clip, start_s=30.0, length_s=None, keyframes_only=True, fps=None, gpu="NVIDIA", gpu_device_path="cuda:0"
    )
    assert [r[0] for r in gpu] == [r[0] for r in cpu]
    assert all(abs(a[2] - b[2]) < 3 for a, b in zip(cpu, gpu, strict=True))
