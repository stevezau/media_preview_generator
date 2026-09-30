"""Run Plex's loudnorm analysis of one audio stream and turn its report into Plex's ``ln:*`` fields."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable

from ..markers.freeze import Freeze
from ..markers.probe import kill_and_collect

# The filter Plex Media Server 1.43 runs for each audio stream (``Plex Transcoder -i FILE -map 0:N -af ... -f null -``).
LOUDNORM_FILTER = "loudnorm=I=-16:TP=-1:LRA=9:print_format=json"
# Plex decodes EAC3 with its Dolby decoder (``-eae_prefix``, only inside Plex), which applies no dynamic range
# compression; ffmpeg's EAC3 decoder does unless told not to. AC3 goes through ffmpeg's decoder in Plex too, DRC on.
NO_DRC_CODECS = frozenset({"eac3"})
# The version Plex writes beside its fields (``ln:loudnessAnalysisVersion``).
ANALYSIS_VERSION = "0.02"
# loudnorm's report key → Plex's field.
_FIELDS = {
    "input_i": "ln:loudness",
    "input_tp": "ln:peak",
    "input_lra": "ln:lra",
    "input_thresh": "ln:threshold",
    "target_offset": "ln:gainOffset",
}
# A stalled mount must not hold a worker for ever; a long file still gets well past real time.
MIN_TIMEOUT_S = 600.0
MAX_TIMEOUT_S = 4 * 3600.0
_POLL_S = 0.5
KILL_WAIT_S = 5.0
REAPER = "loudness-reaper"


class LoudnessError(Exception):
    """ffmpeg failed, timed out, was cancelled, or printed no loudnorm report."""


def command(ffmpeg: str, path: str, index: int, codec: str = "") -> list[str]:
    """The ffmpeg command for one stream: Plex's own, on the CPU (audio decoding gains nothing from a GPU).

    Args:
        ffmpeg: The ffmpeg binary.
        path: The media file.
        index: The stream's index in the file (Plex's ``media_streams.index``).
        codec: The stream's codec (``media_streams.codec``); EAC3 is decoded without DRC, as Plex's decoder does.

    Returns:
        The argument list.
    """
    return [
        ffmpeg,
        "-hide_banner",
        "-nostats",
        *(["-drc_scale", "0"] if codec in NO_DRC_CODECS else []),
        "-i",
        path,
        "-map",
        f"0:{index}",
        "-af",
        LOUDNORM_FILTER,
        "-f",
        "null",
        "-",
    ]


def timeout_for(duration_ms: int | None) -> float:
    """Three times the file's length, kept between ``MIN_TIMEOUT_S`` and ``MAX_TIMEOUT_S``."""
    return min(MAX_TIMEOUT_S, max(MIN_TIMEOUT_S, 3 * (duration_ms or 0) / 1000))


def parse(stderr: str) -> dict[str, str]:
    """The loudnorm report: the last JSON object ffmpeg printed.

    Raises:
        LoudnessError: No report in the output.
    """
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start < 0 or end < start:
        raise LoudnessError("ffmpeg printed no loudnorm report")
    try:
        report = json.loads(stderr[start : end + 1])
    except ValueError as exc:
        raise LoudnessError("ffmpeg printed an unreadable loudnorm report") from exc
    return report


def ln_fields(report: dict[str, str]) -> dict[str, str]:
    """Plex's ``ln:*`` fields from a loudnorm report: its values as printed (two decimals), as Plex stores them.

    Raises:
        LoudnessError: A value is missing or not a finite number (silence reports ``-inf``).
    """
    fields = {"ln:loudnessAnalysisVersion": ANALYSIS_VERSION}
    for key, name in _FIELDS.items():
        try:
            value = float(report[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise LoudnessError(f"loudnorm report lacks a usable {key}") from exc
        if value != value or value in (float("inf"), float("-inf")):
            raise LoudnessError(f"loudnorm measured no usable {key} ({report[key]}); the stream may be silent")
        fields[name] = str(report[key])
    return fields


def run(
    ffmpeg: str,
    path: str,
    index: int,
    *,
    duration_ms: int | None,
    codec: str = "",
    cancel_check: Callable[[], bool] | None = None,
    pause_check: Callable[[], bool] | Freeze | None = None,
) -> dict[str, str]:
    """Analyse one stream and return its ``ln:*`` fields.

    Args:
        ffmpeg: The ffmpeg binary.
        path: The media file (read only).
        index: The stream's index in the file.
        duration_ms: The file's length, for the time limit.
        codec: The stream's codec (see ``command``).
        cancel_check: True once the job is cancelled; ffmpeg is killed.
        pause_check: True while everything is paused: ffmpeg is stopped where it is and the time limit moves out.

    Raises:
        LoudnessError: ffmpeg failed, timed out, was cancelled, or its report was unusable.
    """
    name = os.path.basename(path)
    timeout_s = timeout_for(duration_ms)
    freeze = Freeze.of(pause_check)
    if freeze.hold(cancel_check=cancel_check, name=name) and cancel_check and cancel_check():
        raise LoudnessError(f"Loudness analysis of {name} cancelled")
    # Its own session, so a pause stops ffmpeg's whole group and never the app's.
    proc = subprocess.Popen(
        command(ffmpeg, path, index, codec), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True
    )
    deadline = freeze.clock() + timeout_s
    try:
        while True:
            try:
                _out, err = proc.communicate(timeout=_POLL_S)
                break
            except subprocess.TimeoutExpired:
                freeze.hold(proc, cancel_check=cancel_check, name=name)
                cancelled = bool(cancel_check and cancel_check())
                if cancelled or freeze.clock() > deadline:
                    why = "cancelled" if cancelled else f"timed out after {timeout_s:.0f} s"
                    raise LoudnessError(f"Loudness analysis of {name} {why}") from None
    except BaseException:
        kill_and_collect(proc, what=f"ffmpeg analysing loudness of {name}", reaper_name=REAPER, wait_s=KILL_WAIT_S)
        raise
    text = (err or b"").decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise LoudnessError(f"ffmpeg exited {proc.returncode} analysing {name} stream {index}: {text.strip()[-200:]}")
    return ln_fields(parse(text))
