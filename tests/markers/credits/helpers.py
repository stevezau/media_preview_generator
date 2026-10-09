"""Shared builders for the credits tests: a fake ffmpeg child process, keyframe rows for rule J, and the GPU render node
the hardware integration tests look for."""

from __future__ import annotations

import glob
import pathlib
import sys
import textwrap

from media_preview_generator.markers.credits import rule_j

RENDER = "/dev/dri/renderD128"

VERDICT = "Your platform doesn't support hardware accelerated AV1 decoding."
VENDORS = [("NVIDIA", "cuda:0", "cuda"), ("INTEL", RENDER, "vaapi"), ("AMD", "/dev/dri/renderD129", "vaapi")]

VAAPI_DRIVERS = {"i915": "INTEL", "xe": "INTEL", "amdgpu": "AMD", "radeon": "AMD"}


def vaapi_node() -> tuple[str, str] | None:
    """A render node this decode can use, with its GPU type (NVIDIA's node has no VAAPI decode)."""
    for node in sorted(glob.glob("/dev/dri/renderD*")):
        driver = pathlib.Path(f"/sys/class/drm/{pathlib.Path(node).name}/device/driver")
        gpu = VAAPI_DRIVERS.get(driver.resolve().name) if driver.exists() else None
        if gpu:
            return node, gpu
    return None


def cards(boxes: int) -> tuple[rule_j.Box, ...]:
    """Text boxes stacked down the frame, one per box, centred: where a row with this many boxes might hold them.

    Version 3 reads them in three places (:class:`TestWhereRuleJReadsPositions`, :class:`TestOverlayBoxes`). Centred
    is the roll's own band, so these rows carry text the band steps can reach for; and box *n* is in the same place
    on every row these make, which to :func:`rule_j.overlay_boxes` is a channel bug -- text right across the story in
    one spot. That is why a tail of them carrying text all through has no answer (:class:`TestTextAllThrough`'s
    subtitled rows), and why a fixture that wants two different bands must move its boxes (:func:`band`).
    """
    return tuple((40, 20 + 30 * n, 280, 44 + 30 * n) for n in range(boxes))


def dark(t: float, boxes: int = 0, luma: float = 10.0) -> rule_j.Row:
    return (t, boxes, luma, cards(boxes))


def bright(t: float, boxes: int = 0, luma: float = 120.0) -> rule_j.Row:
    return (t, boxes, luma, cards(boxes))


def caption(t: float, boxes: int, i: int, luma: float = 120.0) -> rule_j.Row:
    """A lit keyframe with ``boxes`` caption boxes wherever keyframe ``i`` puts them: a variety show's captions wander
    over the frame, so no place holds them long enough to be an overlay (:func:`rule_j.overlay_boxes`)."""
    placed = []
    for k in range(boxes):
        x, y = (i * 97 + k * 131) % 240 + 10, (i * 53 + k * 71) % 150 + 5
        placed.append((x, y, x + 60, y + 12))
    return (float(t), boxes, luma, tuple(placed))


def captioned_tail(story_every: int, blank_s: int) -> list[rule_j.Row]:
    """A 1 s keyframe tail: 200 s of story with a caption on one keyframe in ``story_every``; then 220 s where a
    three-box caption lands every 10 s (a credit frame each, 10 s apart, so the 24 s join chains them), one-box
    captions on the keyframes between, and ``blank_s`` of them without any text, in the second half of each 10 s from
    1220 s on; then 28 s of dark cards."""
    rows = [caption(t, 1 if i % story_every == 0 else 0, i) for i, t in enumerate(range(1000, 1200))]
    left = blank_s
    for i, t in enumerate(range(1200, 1420)):
        if t % 10 == 0:
            rows.append(caption(t, 3, i + 500))
        elif t >= 1220 and t % 10 >= 5 and left > 0:
            rows.append(caption(t, 0, i))
            left -= 1
        else:
            rows.append(caption(t, 1, i + 900))
    return rows + [dark(t, 4) for t in range(1420, 1448)]


def cant_decode_lines(hwaccel: str, packets: int = 3) -> str:
    """What ffmpeg's own AV1 decoder says, once per packet to the end of the file, on a GPU without AV1 decode
    (ffmpeg 8.0.1 on an NVIDIA GPU: 6 lines a packet, 3,756 lines in 1.5 s; "Hardware is lacking" is
    NVDEC's own line and is left out)."""
    packet = (
        f"[av1 @ 0x58ba5204abc0] Failed setup for format {hwaccel}: hwaccel initialisation returned error.\n"
        f"[av1 @ 0x58ba5204abc0] {VERDICT}\n"
        "[av1 @ 0x58ba5204abc0] Failed to get pixel format.\n"
        "[av1 @ 0x58ba5204abc0] Get current frame error\n"
        "[vist#0:0/av1 @ 0x58ba520356c0] [dec:av1 @ 0x58ba5204a680] Error submitting packet to decoder: Function not "
        "implemented\n"
    )
    return "  Stream #0:0: Video: av1 (libdav1d) (Main), yuv420p(tv), 1280x720\n" + packet * packets


def fake_ffmpeg(
    frame_values: list[int],
    pts: list[str],
    *,
    exit_code: int = 0,
    sleep_s: float = 0.0,
    extra_bytes: int = 0,
    pid_file: str = "",
    child_pid_file: str = "",
    close_stdout: bool = False,
    linger_s: float = 0.0,
    ignore_sigterm: bool = False,
    progress_file: str = "",
    width: int = 320,
    height: int = 180,
    stderr_tail: str = "",
    stderr_head: str = "",
    head_wait_s: float = 0.0,
) -> list[str]:
    """A child that writes NV12 frames (Y plane filled with each value) to stdout and showinfo lines to stderr.

    ``pts`` entries past the frames become showinfo lines with no whole frame behind them (ffmpeg dying mid-write);
    ``child_pid_file`` spawns a grandchild in the same process group, so the group kill can be asserted;
    ``ignore_sigterm`` stands in for an ffmpeg that won't take a polite signal; ``progress_file`` records how many
    frames have been written, so how far the decoder ran ahead of text detection can be read; ``width`` and ``height``
    are the frames' size; ``stderr_tail`` is written to stderr after the frames (what ffmpeg says as it exits);
    ``stderr_head`` is written before them, which then wait ``head_wait_s`` (what a decoder says before any frame).
    """
    script = textwrap.dedent(f"""
        import os, signal, subprocess, sys, time
        if {ignore_sigterm!r}:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if {pid_file!r}:
            open({pid_file!r}, "w").write(str(os.getpid()))
        if {child_pid_file!r}:
            child = subprocess.Popen(["sleep", "30"])
            open({child_pid_file!r}, "w").write(str(child.pid))
        out = sys.stdout.buffer
        pts = {pts!r}

        def line(i):
            sys.stderr.write("[Parsed_showinfo_3 @ 0x1] n:%d pts:%d pts_time:%-7s duration:1\\n" % (i, i, pts[i]))

        def progress(n):
            if {progress_file!r}:
                open({progress_file!r} + ".tmp", "w").write(str(n))
                os.replace({progress_file!r} + ".tmp", {progress_file!r})

        sys.stderr.write({stderr_head!r})
        sys.stderr.flush()
        time.sleep({head_wait_s})
        for i, value in enumerate({frame_values!r}):
            if i < len(pts):
                line(i)
            out.write(bytes([value]) * ({width} * {height}) + bytes([128]) * ({width} * {height} // 2))
            out.flush()
            progress(i + 1)
            time.sleep({sleep_s})
        for i in range(len({frame_values!r}), len(pts)):
            line(i)
        out.write(b"x" * {extra_bytes})
        out.flush()
        sys.stderr.write({stderr_tail!r})
        if {close_stdout!r}:
            os.close(1)
        time.sleep({linger_s})
        sys.exit({exit_code})
    """)
    return [sys.executable, "-c", script]
