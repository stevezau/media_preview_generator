"""Run Plex's loudnorm analysis of one or more audio streams and turn each report into Plex's ``ln:*`` fields."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable

from loguru import logger

from ..markers.freeze import Freeze
from ..markers.probe import _count_stuck, kill_and_collect

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
# A stalled mount must not hold a worker for ever; a long file still gets well past real time. The cap is per track:
# a batch gets the sum of its tracks' limits, the same worst case as analysing them one after another.
MIN_TIMEOUT_S = 600.0
MAX_TIMEOUT_S = 4 * 3600.0
_POLL_S = 0.5
KILL_WAIT_S = 5.0
REAPER = "loudness-reaper"
# Built with the CPU loudness patch; video previews keep the GPU-capable FFmpeg.
_BUNDLED_FFMPEG = "/opt/ffmpeg-loudness/bin/ffmpeg"


def resolve_ffmpeg(fallback: str) -> str:
    """Use the bundled loudness analyser, or the configured FFmpeg outside the image.

    The path is fixed by the image build, never supplied by job input. Explicit
    callers of ``run`` (including the parity harness) retain their chosen binary.
    """
    if os.path.isfile(_BUNDLED_FFMPEG) and os.access(_BUNDLED_FFMPEG, os.X_OK):
        return _BUNDLED_FFMPEG
    return fallback


class LoudnessError(Exception):
    """ffmpeg failed, timed out, was cancelled, or printed no loudnorm report."""


class LoudnessCancelled(LoudnessError):
    """The job was cancelled while analysing audio."""


class LoudnessTimeout(LoudnessError):
    """ffmpeg exceeded its unpaused analysis time limit."""


def command(ffmpeg: str, path: str, index: int, codec: str = "") -> list[str]:
    """The ffmpeg command for one stream: Plex's own, on the CPU (audio decoding gains nothing from a GPU).

    Args:
        ffmpeg: The ffmpeg binary.
        path: The media file.
        index: The stream's index in the file (Plex's ``media_streams.index``).
        codec: The stream's codec (``media_streams.codec``); EAC3 is decoded without DRC, as Plex's decoder does.

    Returns:
        The argument list.

    Raises:
        LoudnessError: The stream index is not a nonnegative integer.
    """
    if type(index) is not int or index < 0:
        raise LoudnessError("The audio stream index must be a nonnegative integer")
    return [
        ffmpeg,
        "-hide_banner",
        "-nostats",
        # Machine-readable progress on stdout; the loudnorm report stays on stderr.
        "-progress",
        "pipe:1",
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


def command_many(ffmpeg: str, path: str, tracks: list[tuple[int, str, int | None]]) -> list[str]:
    """Build one input with an independent, named loudnorm output for each absolute stream index."""
    indices = [index for index, _codec, _duration_ms in tracks]
    if not 2 <= len(indices) <= 3 or any(type(index) is not int or index < 0 for index in indices):
        raise LoudnessError("A loudness batch needs two or three nonnegative stream indices")
    if len(set(indices)) != len(indices):
        raise LoudnessError("A loudness batch cannot repeat a stream index")
    args = [ffmpeg, "-hide_banner", "-nostats", "-progress", "pipe:1", "-filter_threads", "1"]
    for index, codec, _duration_ms in tracks:
        if codec in NO_DRC_CODECS:
            args.extend((f"-drc_scale:{index}", "0"))
    args.extend(("-i", path))
    for index in indices:
        args.extend(
            (
                "-map",
                f"0:{index}",
                "-af",
                LOUDNORM_FILTER.replace("loudnorm=", f"loudnorm@track{index}="),
                "-f",
                "null",
                "-",
            )
        )
    return args


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


_NAMED_PREFIX = re.compile(r"\[loudnorm@track(?P<index>\d+) @ [^\]\n]+\]")


def parse_many(stderr: str, indices: list[int]) -> dict[int, dict[str, str] | LoudnessError]:
    """Attribute each named loudnorm report to exactly one requested absolute stream index.

    A stream whose own report is missing or unreadable comes back as a ``LoudnessError`` value, so its
    siblings' valid reports are kept.

    Args:
        stderr: ffmpeg's stderr from a ``command_many`` run.
        indices: The absolute stream indices that were requested.

    Returns:
        For each requested index, its report or the (unraised) error for that stream.

    Raises:
        LoudnessError: A report names an unexpected or repeated stream, so no attribution can be trusted.
    """
    expected = set(indices)
    reports: dict[int, dict[str, str] | LoudnessError] = {}
    for match in _NAMED_PREFIX.finditer(stderr):
        index = int(match["index"])
        if index not in expected or index in reports:
            raise LoudnessError(f"ffmpeg printed an unexpected or duplicate loudnorm report for stream {index}")
        try:
            report_text = stderr[match.end() :].lstrip()
            if not report_text.startswith("{"):
                raise ValueError("the named report is missing its JSON object")
            report, _end = json.JSONDecoder().raw_decode(report_text)
        except ValueError as exc:
            error = LoudnessError(f"ffmpeg printed an unreadable loudnorm report for stream {index}")
            error.__cause__ = exc
            reports[index] = error
            continue
        if not isinstance(report, dict):
            reports[index] = LoudnessError(f"ffmpeg printed an invalid loudnorm report for stream {index}")
            continue
        reports[index] = report
    for index in sorted(expected - reports.keys()):
        reports[index] = LoudnessError(f"ffmpeg omitted the loudnorm report for stream {index}")
    return reports


def ln_fields(report: dict[str, str]) -> dict[str, str]:
    """Plex's ``ln:*`` fields from a loudnorm report: its values as printed (two decimals), as Plex stores them.

    Raises:
        LoudnessError: A value is missing or differs from Plex's measured numeric/silence representation.
    """
    fields = {"ln:loudnessAnalysisVersion": ANALYSIS_VERSION}
    for key, name in _FIELDS.items():
        try:
            value = float(report[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise LoudnessError(f"loudnorm report lacks a usable {key}") from exc
        if math.isnan(value):
            raise LoudnessError(f"loudnorm measured no usable {key}")
        fields[name] = str(report[key])
    if not valid_measurements(fields):
        raise LoudnessError("loudnorm reported unsupported loudness measurements")
    return fields


def valid_measurements(fields: dict[str, str]) -> bool:
    """Recognize finite measurements and Plex's verified silent/very-short stream values.

    Plex 1.43.4 stores negative-infinite integrated loudness with positive-infinite
    gain offset for silence and audio too short for integrated measurement. A
    silent stream also has negative-infinite peak; its range and threshold stay finite.
    """
    try:
        values = {name: float(fields[name]) for name in _FIELDS.values()}
    except (KeyError, TypeError, ValueError):
        return False
    if all(math.isfinite(value) for value in values.values()):
        return True
    return (
        values["ln:loudness"] == -math.inf
        and values["ln:gainOffset"] == math.inf
        and (math.isfinite(values["ln:peak"]) or values["ln:peak"] == -math.inf)
        and math.isfinite(values["ln:lra"])
        and math.isfinite(values["ln:threshold"])
    )


def _parse_progress_seconds(value: str) -> float | None:
    """ffmpeg's ``out_time_us``/``out_time_ms`` (both microseconds) as seconds; None for ``N/A`` or junk."""
    try:
        seconds = int(value) / 1_000_000
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _parse_progress_speed(value: str) -> float | None:
    """ffmpeg's ``speed=1.4x`` as 1.4; None for ``N/A`` or junk."""
    try:
        speed = float(value.strip().removesuffix("x"))
    except ValueError:
        return None
    return speed if math.isfinite(speed) and speed > 0 else None


def read_progress(stream, on_progress: Callable[[float, float | None], None] | None) -> None:
    """Drain ffmpeg's ``-progress`` output to its end, reporting each block (``progress=`` line) once.

    Reading to the end matters even with no callback: an undrained pipe would block ffmpeg.

    Args:
        stream: ffmpeg's stdout (binary).
        on_progress: Called with (seconds of audio processed, speed as a multiple of real time or None).
    """
    seconds: float | None = None
    speed: float | None = None
    warned = False
    for raw in stream:
        key, _, value = raw.decode("utf-8", errors="replace").strip().partition("=")
        if key in ("out_time_us", "out_time_ms"):
            seconds = _parse_progress_seconds(value)
        elif key == "speed":
            speed = _parse_progress_speed(value)
        elif key == "progress" and on_progress is not None and seconds is not None:
            try:
                on_progress(seconds, speed)
            except Exception as exc:
                if not warned:
                    warned = True
                    logger.warning("Loudness progress callback failed (further failures this run not logged): {}", exc)


def _read_all(stream, sink: list[bytes]) -> None:
    sink.append(stream.read())


def _join_readers(readers: list[threading.Thread]) -> bool:
    """Join the started readers within one shared ``KILL_WAIT_S``; True when none is still running."""
    stop = time.monotonic() + KILL_WAIT_S
    for reader in readers:
        if reader.is_alive():
            reader.join(timeout=max(0.0, stop - time.monotonic()))
    return not any(reader.is_alive() for reader in readers)


def _reap_without_reading(proc: subprocess.Popen, what: str) -> None:
    """Collect a killed process in the background with ``wait()``: the readers already own its pipes."""
    logger.warning("{} still holds its output after being stopped; leaving it to finish on its own", what)
    _count_stuck(REAPER, 1)

    def reap() -> None:
        try:
            proc.wait()
        finally:
            _count_stuck(REAPER, -1)

    try:
        threading.Thread(target=reap, daemon=True, name=REAPER).start()
    except RuntimeError as exc:
        _count_stuck(REAPER, -1)
        logger.warning("Couldn't start a thread to collect {}: {}", what, exc)


def _run_command(
    args: list[str],
    path: str,
    *,
    timeout_s: float,
    description: str,
    cancel_check: Callable[[], bool] | None = None,
    pause_check: Callable[[], bool] | Freeze | None = None,
    on_progress: Callable[[float, float | None], None] | None = None,
) -> str:
    """Run an analysis command under the existing pause, cancellation, and reader supervision.

    Args:
        args: The ffmpeg command line.
        path: The media file, for messages.
        timeout_s: Unpaused seconds allowed before the process is killed.
        description: Which stream(s) this is, for messages.
        cancel_check: Returns True when the job was cancelled.
        pause_check: Holds the process while paused.
        on_progress: Called with (seconds of audio processed, speed or None).

    Returns:
        ffmpeg's stderr.

    Raises:
        LoudnessCancelled: The job was cancelled.
        LoudnessTimeout: ffmpeg exceeded ``timeout_s``.
        LoudnessError: ffmpeg could not start, kept its output open, or exited non-zero.
    """
    name = os.path.basename(path)
    freeze = Freeze.of(pause_check)
    if cancel_check and cancel_check():
        raise LoudnessCancelled(f"Loudness analysis of {name} cancelled")
    freeze.hold(cancel_check=cancel_check, name=name)
    if cancel_check and cancel_check():
        raise LoudnessCancelled(f"Loudness analysis of {name} cancelled")
    # Its own session, so a pause stops ffmpeg's whole group and never the app's.
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise LoudnessError(f"Could not start ffmpeg analysing {name}: {exc.strerror or type(exc).__name__}") from exc
    stderr_chunks: list[bytes] = []
    readers = [
        threading.Thread(target=read_progress, args=(proc.stdout, on_progress), daemon=True, name="loudness-progress"),
        threading.Thread(target=_read_all, args=(proc.stderr, stderr_chunks), daemon=True, name="loudness-stderr"),
    ]
    deadline = freeze.clock() + timeout_s
    try:
        for reader in readers:
            reader.start()
        while True:
            try:
                proc.wait(timeout=_POLL_S)
                break
            except subprocess.TimeoutExpired:
                freeze.hold(proc, cancel_check=cancel_check, name=name)
                cancelled = bool(cancel_check and cancel_check())
                if cancelled or freeze.clock() > deadline:
                    if cancelled:
                        raise LoudnessCancelled(f"Loudness analysis of {name} cancelled") from None
                    raise LoudnessTimeout(f"Loudness analysis of {name} timed out after {timeout_s:.0f} s") from None
    except BaseException:
        proc.kill()
        what = f"ffmpeg analysing loudness of {name}"
        if _join_readers(readers):
            kill_and_collect(proc, what=what, reaper_name=REAPER, wait_s=KILL_WAIT_S)
        else:
            # A reader still owns each pipe, so communicate() would read the same pipe from two threads.
            _reap_without_reading(proc, what)
        raise
    if not _join_readers(readers):
        # Only a process the kill never reached (ffmpeg's own children) can hold the pipe after ffmpeg exited.
        raise LoudnessError(f"Loudness analysis of {name} could not read ffmpeg's report: its output stayed open")
    for stream in (proc.stdout, proc.stderr):
        stream.close()
    if cancel_check and cancel_check():
        raise LoudnessCancelled(f"Loudness analysis of {name} cancelled")
    text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise LoudnessError(f"ffmpeg exited {proc.returncode} analysing {name} {description}: {text.strip()[-200:]}")
    return text


def run(
    ffmpeg: str,
    path: str,
    index: int,
    *,
    duration_ms: int | None,
    codec: str = "",
    cancel_check: Callable[[], bool] | None = None,
    pause_check: Callable[[], bool] | Freeze | None = None,
    on_progress: Callable[[float, float | None], None] | None = None,
) -> dict[str, str]:
    """Analyse one stream and return its ``ln:*`` fields.

    Args:
        ffmpeg: The ffmpeg binary.
        path: The media file.
        index: The stream's index in the file.
        duration_ms: The file's length, for the time limit.
        codec: The stream's codec.
        cancel_check: Returns True when the job was cancelled.
        pause_check: Holds the process while paused.
        on_progress: Called with (seconds of audio processed, speed or None).

    Returns:
        The stream's ``ln:*`` fields.

    Raises:
        LoudnessCancelled: The job was cancelled.
        LoudnessTimeout: ffmpeg exceeded its time limit.
        LoudnessError: ffmpeg failed or printed no usable report.
    """
    text = _run_command(
        command(ffmpeg, path, index, codec),
        path,
        timeout_s=timeout_for(duration_ms),
        description=f"stream {index}",
        cancel_check=cancel_check,
        pause_check=pause_check,
        on_progress=on_progress,
    )
    return ln_fields(parse(text))


def run_many(
    ffmpeg: str,
    path: str,
    tracks: list[tuple[int, str, int | None]],
    *,
    cancel_check: Callable[[], bool] | None = None,
    pause_check: Callable[[], bool] | Freeze | None = None,
    on_progress: Callable[[float, float | None], None] | None = None,
) -> dict[int, dict[str, str] | LoudnessError]:
    """Analyse two or three streams in one input pass, validating each stream's report on its own.

    Args:
        ffmpeg: The ffmpeg binary.
        path: The media file.
        tracks: ``(stream index, codec, duration_ms)`` for each stream.
        cancel_check: Returns True when the job was cancelled.
        pause_check: Holds the process while paused.
        on_progress: Called with (seconds of audio processed, speed or None).

    Returns:
        For each stream, its ``ln:*`` fields, or the (unraised) error for that stream alone.

    Raises:
        LoudnessCancelled: The job was cancelled.
        LoudnessTimeout: ffmpeg exceeded the sum of its tracks' time limits.
        LoudnessError: The batch is invalid, ffmpeg failed, or its reports can't be attributed.
    """
    args = command_many(ffmpeg, path, tracks)
    indices = [index for index, _codec, _duration_ms in tracks]
    text = _run_command(
        args,
        path,
        timeout_s=sum(timeout_for(duration_ms) for _index, _codec, duration_ms in tracks),
        description=f"streams {indices}",
        cancel_check=cancel_check,
        pause_check=pause_check,
        on_progress=on_progress,
    )
    results: dict[int, dict[str, str] | LoudnessError] = {}
    for index, report in parse_many(text, indices).items():
        if isinstance(report, LoudnessError):
            results[index] = report
            continue
        try:
            results[index] = ln_fields(report)
        except LoudnessError as exc:
            results[index] = exc
    return results
