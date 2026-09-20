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


@pytest.fixture(scope="module")
def intra_clips(tmp_path_factory):
    """The same 20 s of moving picture as an all-I H.264 at 24 fps, an MJPEG at 25 fps, and a 2 s GOP H.264."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("no ffmpeg")
    root = tmp_path_factory.mktemp("intra")
    encodes = {
        "all-i.mkv": (24, ["-c:v", "libx264", "-g", "1", "-pix_fmt", "yuv420p"]),
        "mjpeg.avi": (25, ["-c:v", "mjpeg", "-q:v", "5", "-pix_fmt", "yuvj420p"]),
        "gop48.mkv": (24, ["-c:v", "libx264", "-g", "48", "-pix_fmt", "yuv420p"]),
    }
    for name, (rate, codec) in encodes.items():
        subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate={rate}:duration=20",
                        *codec, str(root / name)], check=True)  # fmt: skip
    # Two video streams: an MJPEG thumbnail track first, the long-GOP main picture second and default. ffmpeg decodes
    # the main one; the stride is measured on the first.
    two = root / "two-streams.mkv"
    subprocess.run([ffmpeg, "-v", "error", "-i", str(root / "mjpeg.avi"), "-i", str(root / "gop48.mkv"), "-map", "0:v",
                    "-map", "1:v", "-c", "copy", "-disposition:v:0", "0", "-disposition:v:1", "default", str(two)],
                   check=True)  # fmt: skip
    return ffmpeg, {name: str(root / name) for name in [*encodes, "two-streams.mkv"]}


@pytest.fixture(scope="module")
def vp9_clips(tmp_path_factory):
    """The same 20 s of moving picture as VP9: a 2 s GOP in WebM and in Matroska, every frame a keyframe, and a single
    keyframe at 0 s."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("no ffmpeg")
    encoders = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    if "libvpx-vp9" not in encoders:
        pytest.skip("no VP9 encoder")
    root = tmp_path_factory.mktemp("vp9")
    vp9 = ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-b:v", "1M"]
    encodes = {
        "gop48.webm": [*vp9, "-g", "48", "-keyint_min", "48"],
        "gop48.mkv": [*vp9, "-g", "48", "-keyint_min", "48"],
        "all-key.webm": [*vp9, "-g", "1"],
        "one-key.webm": [*vp9, "-g", "9999", "-keyint_min", "9999"],
    }
    for name, codec in encodes.items():
        subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=20",
                        *codec, str(root / name)], check=True)  # fmt: skip
    return ffmpeg, {name: str(root / name) for name in encodes}


def _thinned_rows(ffmpeg, path, thinning, **kwargs):
    return _intra_rows(ffmpeg, path, keep_every=thinning.keep_every, drop_non_key=thinning.drop_non_key, **kwargs)


def _pixels(planes):
    """Stands in for text detection: each frame's pixel sum, so a decoded frame can be compared exactly."""
    return [int(plane.sum()) for plane in planes]


def _intra_rows(ffmpeg, path, **kwargs):
    return frames.decode_rows(path, ffmpeg=ffmpeg, start_s=3.0, length_s=None, keyframes_only=True, fps=None,
                              detect_boxes=_pixels, **kwargs)  # fmt: skip


def _rows(clip, **kwargs):
    ffmpeg, path = clip
    return frames.decode_rows(path, ffmpeg=ffmpeg, detect_boxes=lambda planes: [()] * len(planes), **kwargs)


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


@pytest.mark.parametrize(("name", "stride"), [("all-i.mkv", 48), ("mjpeg.avi", 50), ("gop48.mkv", None)])
def test_intra_only_streams_are_told_from_their_packets(intra_clips, name, stride):
    ffmpeg, clips = intra_clips
    assert frames.keyframe_thinning(clips[name], ffmpeg) == frames.KeyframeThinning(stride, False)


@pytest.mark.parametrize(("name", "stride"), [("all-i.mkv", 48), ("mjpeg.avi", 50)])
def test_a_thinned_pass_decodes_one_frame_per_two_seconds_untouched(intra_clips, name, stride):
    ffmpeg, clips = intra_clips
    every = _intra_rows(ffmpeg, clips[name], gpu=None, gpu_device_path=None)
    thinned = _intra_rows(ffmpeg, clips[name], gpu=None, gpu_device_path=None, keep_every=stride)
    assert len(every) == 17 * stride // 2  # all of them, 3–20 s: skip_frame nokey skips nothing here
    assert [row[0] for row in thinned] == [3.0 + 2 * i for i in range(9)]
    # Packets are dropped, never altered: each kept frame decodes to exactly what it is when every frame is read.
    by_pts = {row[0]: row for row in every}
    assert thinned == [by_pts[row[0]] for row in thinned]


def test_only_the_measured_stream_is_thinned(intra_clips):
    # The first video stream is intra-only, but ffmpeg decodes the other one: its keyframes must all be read, or the
    # pass would find no frames and store "no credits" for the file.
    ffmpeg, clips = intra_clips
    path = clips["two-streams.mkv"]
    assert frames.keyframe_thinning(path, ffmpeg) == frames.KeyframeThinning(50, False)
    every = _intra_rows(ffmpeg, path, gpu=None, gpu_device_path=None)
    assert [row[0] for row in every] == [4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0]  # the 2 s GOP's keyframes
    assert _intra_rows(ffmpeg, path, gpu=None, gpu_device_path=None, keep_every=50) == every


@pytest.mark.gpu
def test_cuda_thins_an_intra_only_pass_the_same_way(intra_clips):
    if shutil.which("nvidia-smi") is None:
        pytest.skip("no NVIDIA GPU")
    ffmpeg, clips = intra_clips
    cpu = _intra_rows(ffmpeg, clips["all-i.mkv"], gpu=None, gpu_device_path=None, keep_every=48)
    gpu = _intra_rows(ffmpeg, clips["all-i.mkv"], gpu="NVIDIA", gpu_device_path="cuda:0", keep_every=48)
    assert [r[0] for r in gpu] == [r[0] for r in cpu] == [3.0 + 2 * i for i in range(9)]
    assert all(abs(a[2] - b[2]) < 3 for a, b in zip(cpu, gpu, strict=True))


@pytest.mark.gpu
def test_vaapi_thins_an_intra_only_pass_the_same_way(intra_clips):
    node = vaapi_node()
    if node is None:
        pytest.skip("no Intel or AMD render node")
    device, vendor = node
    ffmpeg, clips = intra_clips
    cpu = _intra_rows(ffmpeg, clips["all-i.mkv"], gpu=None, gpu_device_path=None, keep_every=48)
    gpu = _intra_rows(ffmpeg, clips["all-i.mkv"], gpu=vendor, gpu_device_path=device, keep_every=48)
    assert [r[0] for r in gpu] == [r[0] for r in cpu] == [3.0 + 2 * i for i in range(9)]
    assert all(abs(a[2] - b[2]) < 3 for a, b in zip(cpu, gpu, strict=True))


VP9_KEYFRAMES = [4.0 + 2 * i for i in range(8)]  # a keyframe every 2 s, read from 3 s


@pytest.mark.parametrize(
    ("name", "thinning"),
    [("gop48.webm", (None, True)), ("gop48.mkv", (None, True)), ("all-key.webm", (48, True)),
     ("one-key.webm", (None, True))],
)  # fmt: skip
def test_a_vp9_stream_is_told_from_the_packet_probe(vp9_clips, name, thinning):
    ffmpeg, clips = vp9_clips
    assert frames.keyframe_thinning(clips[name], ffmpeg) == frames.KeyframeThinning(*thinning)


@pytest.mark.parametrize("name", ["gop48.webm", "gop48.mkv"])
def test_a_vp9_keyframe_pass_reads_its_keyframes_untouched(vp9_clips, name):
    # VP9's decoder ignores -skip_frame nokey: without the drop the pass returns every frame of the window.
    ffmpeg, clips = vp9_clips
    every = _intra_rows(ffmpeg, clips[name], gpu=None, gpu_device_path=None)
    keyframes = _thinned_rows(ffmpeg, clips[name], frames.keyframe_thinning(clips[name], ffmpeg), gpu=None,
                              gpu_device_path=None)  # fmt: skip
    assert len(every) == 17 * 24
    assert [row[0] for row in keyframes] == VP9_KEYFRAMES
    by_pts = {row[0]: row for row in every}
    assert keyframes == [by_pts[row[0]] for row in keyframes]


def test_an_all_key_vp9_stream_is_thinned_to_one_frame_per_two_seconds(vp9_clips):
    # Both drops in one -bsf:V:0: every packet is a keyframe, so the stride does the thinning.
    ffmpeg, clips = vp9_clips
    rows = _thinned_rows(ffmpeg, clips["all-key.webm"], frames.KeyframeThinning(48, True), gpu=None,
                         gpu_device_path=None)  # fmt: skip
    assert [row[0] for row in rows] == [3.0 + 2 * i for i in range(9)]


def test_a_vp9_tail_without_a_keyframe_reads_no_frames_on_the_cpu(vp9_clips):
    # Every packet is dropped and ffmpeg still exits 0: no rows, which rule J reads as a tail without a roll.
    ffmpeg, clips = vp9_clips
    assert _thinned_rows(ffmpeg, clips["one-key.webm"], frames.KeyframeThinning(None, True), gpu=None,
                         gpu_device_path=None) == []  # fmt: skip


@pytest.mark.gpu
def test_cuda_reads_a_vp9_keyframe_pass_the_same_way(vp9_clips):
    if shutil.which("nvidia-smi") is None:
        pytest.skip("no NVIDIA GPU")
    ffmpeg, clips = vp9_clips
    drop = frames.KeyframeThinning(None, True)
    cpu = _thinned_rows(ffmpeg, clips["gop48.webm"], drop, gpu=None, gpu_device_path=None)
    gpu = _thinned_rows(ffmpeg, clips["gop48.webm"], drop, gpu="NVIDIA", gpu_device_path="cuda:0")
    assert [r[0] for r in gpu] == [r[0] for r in cpu] == VP9_KEYFRAMES
    assert all(abs(a[2] - b[2]) < 3 for a, b in zip(cpu, gpu, strict=True))
    # No keyframe in the window: a GPU failure, so the worker reruns the file on the CPU (which reads no roll).
    with pytest.raises(frames.GpuDecodeError, match="decoded no frames"):
        _thinned_rows(ffmpeg, clips["one-key.webm"], drop, gpu="NVIDIA", gpu_device_path="cuda:0")


@pytest.mark.gpu
def test_vaapi_reads_a_vp9_keyframe_pass_the_same_way(vp9_clips):
    # Intel and AMD only: storage has an NVIDIA render node, so this skips there and runs in the lab image.
    node = vaapi_node()
    if node is None:
        pytest.skip("no Intel or AMD render node")
    device, vendor = node
    ffmpeg, clips = vp9_clips
    drop = frames.KeyframeThinning(None, True)
    cpu = _thinned_rows(ffmpeg, clips["gop48.webm"], drop, gpu=None, gpu_device_path=None)
    gpu = _thinned_rows(ffmpeg, clips["gop48.webm"], drop, gpu=vendor, gpu_device_path=device)
    assert [r[0] for r in gpu] == [r[0] for r in cpu] == VP9_KEYFRAMES
    assert all(abs(a[2] - b[2]) < 3 for a, b in zip(cpu, gpu, strict=True))
    with pytest.raises(frames.GpuDecodeError, match="decoded no frames"):
        _thinned_rows(ffmpeg, clips["one-key.webm"], drop, gpu=vendor, gpu_device_path=device)
