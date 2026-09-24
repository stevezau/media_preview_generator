#!/usr/bin/env python3
"""The VP9 keyframe pass on every GPU vendor, inside a throwaway app container on the plex host.

    python3 plex_vp9_keyframes.py [VENDOR ...]     prints one JSON result; each VENDOR (NVIDIA, INTEL, AMD) must be here

VP9's decoder ignores ``-skip_frame nokey``, so the keyframe pass drops the packets not flagged as keyframes before
the decoder (``frames.keyframe_thinning`` → ``drop_non_key``). This encodes a 30 s VP9 clip (24 fps, a keyframe every
48 frames) into a temporary folder inside the container, builds the app's keyframe-pass command for each device the
app's hwaccel builder can use here (``frames.decode_command``: CUDA for NVIDIA, VAAPI for Intel and AMD render nodes),
and counts the frames that come out of a 20 s window. Two controls run beside it: the same command without the drop,
which must give more frames, and on a GPU the same command without its hwaccel arguments, which must fail, since the
GPU scale filter only takes GPU surfaces; so a GPU run that exits 0 decoded on the GPU. Nothing outside the
container's own temporary folder is written.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from media_preview_generator.markers.credits import frames
from media_preview_generator.markers.probe import ffprobe_path_for, video_packets
from media_preview_generator.processing.hwaccel import hwaccel_decode_args

FFMPEG = "ffmpeg"
WINDOW_START_S = 10.0
WINDOW_S = 20.0
VAAPI_VENDORS = {"0x8086": "INTEL", "0x1002": "AMD"}


def render_nodes() -> dict[str, str]:
    """Each VAAPI vendor's first render node, by its sysfs vendor id.

    Returns:
        ``{"INTEL": "/dev/dri/renderD128", ...}``, only for the vendors present.
    """
    nodes: dict[str, str] = {}
    for vendor in sorted(Path("/sys/class/drm").glob("renderD*/device/vendor")):
        name = VAAPI_VENDORS.get(vendor.read_text().strip())
        if name:
            nodes.setdefault(name, f"/dev/dri/{vendor.parent.parent.name}")
    return nodes


def encode_clip(folder: Path) -> Path:
    """A 30 s 640×360 VP9 clip at 24 fps with a keyframe every 48 frames.

    Args:
        folder: Where the clip goes (a temporary folder).

    Returns:
        The clip's path.

    Raises:
        subprocess.CalledProcessError: The encode failed.
    """
    clip = folder / "vp9-g48.mkv"
    subprocess.run([FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc2=size=640x360:rate=24", "-t", "30", "-c:v", "libvpx-vp9", "-g", "48",
                    "-b:v", "1M", "-cpu-used", "5", "-row-mt", "1", "-threads", "2", "-an", str(clip)],
                   check=True)  # fmt: skip
    return clip


def keyframe_times(clip: Path) -> list[float]:
    """The times of the packets the container flags as keyframes, from ffprobe.

    Args:
        clip: The clip.

    Returns:
        Seconds, in order.
    """
    out = subprocess.run([ffprobe_path_for(FFMPEG), "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "packet=pts_time,flags", "-of", "csv=p=0", str(clip)],
                         capture_output=True, text=True, check=True).stdout  # fmt: skip
    times = []
    for line in out.splitlines():
        pts, _, flags = line.partition(",")
        if "K" in flags:
            times.append(round(float(pts), 3))
    return times


def keyframe_pass(clip: Path, gpu: str | None, device: str | None, thinning: frames.KeyframeThinning) -> list[str]:
    """The app's keyframe-pass command over the window.

    Args:
        clip: The clip.
        gpu: The worker's GPU type, None for the CPU.
        device: The worker's device.
        thinning: What to drop before the decoder.

    Returns:
        The argv.
    """
    command, _ = frames.decode_command(
        FFMPEG, str(clip), start_s=WINDOW_START_S, length_s=WINDOW_S, keyframes_only=True, fps=None, gpu=gpu,
        gpu_device_path=device, keep_every=thinning.keep_every, drop_non_key=thinning.drop_non_key,
        download_format=thinning.download_format or "nv12",  # the clips are 8-bit 4:2:0
    )  # fmt: skip
    return command


def without_hwaccel(command: list[str], gpu: str, device: str) -> list[str]:
    """The same command with the device's hwaccel arguments taken out, the GPU download filter left in.

    Args:
        command: A GPU keyframe-pass command.
        gpu: Its GPU type.
        device: Its device.

    Returns:
        The argv.

    Raises:
        ValueError: The hwaccel arguments aren't where ``decode_command`` puts them.
    """
    hw = list(hwaccel_decode_args(gpu, device, keep_on_gpu=True).args)
    at = command.index("-threads") + 2
    if not hw or command[at : at + len(hw)] != hw:
        raise ValueError(f"no {hw} after -threads in {command}")
    return command[:at] + command[at + len(hw) :]


def run(command: list[str]) -> dict:
    """Run one ffmpeg command and describe what came out.

    Args:
        command: The argv.

    Returns:
        Its input arguments (hwaccel, packet drop, ``-skip_frame``), its exit code, the frames written (raw bytes and
        ``showinfo`` lines), how many of them ``showinfo`` marks ``iskey:1``, their times, and the tail of stderr when
        it failed.
    """
    proc = subprocess.run(command, capture_output=True, timeout=300)
    info = [line for line in proc.stderr.splitlines() if b"pts_time:" in line]
    iskey = [re.search(rb"iskey:(\d)", line) for line in info]
    result = {
        "input_args": " ".join(command[command.index("-threads") + 2 : command.index("-ss")]),
        "exit": proc.returncode,
        "frames_raw": len(proc.stdout) // (frames.FRAME_W * frames.FRAME_H * 3 // 2),
        "frames_showinfo": len(info),
        "iskey_1": sum(1 for match in iskey if match and match[1] == b"1"),
        "pts": [round(float(re.search(rb"pts_time:(\S+)", line)[1]), 3) for line in info],
    }
    if proc.returncode != 0:
        result["stderr_tail"] = proc.stderr.decode("utf-8", "replace").strip()[-300:]
    return result


def device_runs(clip: Path, gpu: str | None, device: str | None, thinning: frames.KeyframeThinning) -> dict:
    """The keyframe pass on one device and its controls.

    Args:
        clip: The clip.
        gpu: The worker's GPU type, None for the CPU.
        device: The worker's device.
        thinning: What ``keyframe_thinning`` answered for the clip.

    Returns:
        ``with_drop`` (the app's command), ``without_drop``, and on a GPU ``software_frames`` (no hwaccel arguments).
    """
    command = keyframe_pass(clip, gpu, device, thinning)
    runs = {"with_drop": run(command), "without_drop": run(keyframe_pass(clip, gpu, device, thinning._replace(
        drop_non_key=False)))}  # fmt: skip
    if gpu is not None:
        runs["software_frames"] = run(without_hwaccel(command, gpu, device))
    return runs


def main(required: list[str]) -> int:
    """Encode the clip, run every device with its controls, and print the JSON result.

    Args:
        required: Vendors that must be present (a missing one fails the run instead of being skipped).

    Returns:
        0 when every check holds, 1 otherwise.
    """
    devices: dict[str, tuple[str | None, str | None]] = {"CPU": (None, None)}
    if Path("/dev/nvidiactl").exists():
        devices["NVIDIA"] = ("NVIDIA", "cuda:0")
    devices.update({vendor: (vendor, node) for vendor, node in render_nodes().items()})
    with tempfile.TemporaryDirectory(prefix="vp9-") as folder:
        clip = encode_clip(Path(folder))
        probed = video_packets(str(clip), ffprobe=ffprobe_path_for(FFMPEG), packets=frames.INTRA_CHECK_PACKETS)
        thinning = frames.keyframe_thinning(str(clip), FFMPEG)
        keys = keyframe_times(clip)
        expected = [t for t in keys if WINDOW_START_S <= t < WINDOW_START_S + WINDOW_S]
        runs = {name: device_runs(clip, gpu, device, thinning) for name, (gpu, device) in devices.items()}
    for name, device in runs.items():
        kept = device["with_drop"]
        device["checks"] = {
            "the app's command gave one frame per flagged keyframe, all iskey:1, at their times": kept["exit"] == 0
            and kept["frames_raw"] == len(expected) and kept["iskey_1"] == len(expected) and kept["pts"] == expected,
            "without the drop more frames came out": device["without_drop"]["exit"] == 0
            and device["without_drop"]["frames_raw"] > len(expected),
        }  # fmt: skip
        if name != "CPU":
            device["checks"]["without hwaccel arguments the GPU scale filter failed"] = (
                device["software_frames"]["exit"] != 0 and device["software_frames"]["frames_raw"] == 0
            )
    missing = [vendor for vendor in required if vendor not in devices]
    result = {
        "codec": probed.codec,
        "thinning": thinning._asdict(),
        "flagged_keyframes": keys,
        "window_s": [WINDOW_START_S, WINDOW_START_S + WINDOW_S],
        "expected_frames": len(expected),
        "absent_vendors": [v for v in ("NVIDIA", "INTEL", "AMD") if v not in devices],
        "checks": {
            "keyframe_thinning drops non-key packets and nothing else": thinning.drop_non_key
            and thinning.keep_every is None,
            "the window holds flagged keyframes": bool(expected),
            f"every required vendor was present ({', '.join(required) or 'none required'})": not missing,
            "every device passed its checks": all(all(d["checks"].values()) for d in runs.values()),
        },  # fmt: skip
        "runs": runs,
    }
    result["pass"] = all(result["checks"].values())
    print(json.dumps(result, indent=1))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main([vendor.upper() for vendor in sys.argv[1:]]))
