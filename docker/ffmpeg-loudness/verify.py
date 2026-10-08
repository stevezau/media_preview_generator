#!/usr/bin/env python3
"""Compare stock and candidate FFmpeg loudnorm reports and throughput.

Usage:
    python3 docker/ffmpeg-loudness/verify.py \
        --reference /path/to/stock/ffmpeg --candidate /path/to/patched/ffmpeg

All audio fixtures are synthesized into a temporary directory. No Plex database
or user media is read or written. The host FFmpeg is used only to create WAV and
codec fixtures; both analyzers process those exact same files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FILTER = "loudnorm=I=-16:TP=-1:LRA=9:print_format=json"
JSON_START = re.compile(r"\{\s*\"input_i\"")
REQUIRED_PLEX_FIELDS = {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"}


@dataclass(frozen=True)
class Case:
    name: str
    path: Path
    stream: int = 0
    codec: str = ""


def run(command: list[str], *, capture: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a fixture/analyzer command and include its diagnostics on failure."""
    proc = subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        env={**os.environ, "LC_ALL": "C"},
    )
    if proc.returncode:
        stderr = proc.stderr or ""
        raise RuntimeError(f"Command exited {proc.returncode}: {command!r}\n{stderr[-5000:]}")
    return proc


def make_lavfi(ffmpeg: str, out: Path, source: str) -> None:
    run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", source, "-c:a", "pcm_s16le", str(out)])


def make_fixtures(ffmpeg: str, work: Path, long_seconds: int) -> list[Case]:
    """Create deterministic PCM, codec, and multitrack test inputs."""
    cases: list[Case] = []
    sources = {
        "silence": "anullsrc=r=48000:cl=mono:d=12",
        "short_350ms": "sine=frequency=997:sample_rate=48000:duration=0.35,volume=-20dB",
        # The default sine source is about -18 dBFS, placing this just below
        # the BS.1770 absolute gate after the additional -52 dB attenuation.
        "quiet_near_gate": "sine=frequency=997:sample_rate=48000:duration=12,volume=-52dB",
        "dynamic_noise": (
            "anoisesrc=color=pink:sample_rate=48000:duration=24:amplitude=0.3:seed=20261008,"
            "volume='if(lt(t,6),0.04,if(lt(t,12),0.25,if(lt(t,18),0.06,0.18)))':eval=frame"
        ),
        "stereo": ("aevalsrc=0.2*sin(2*PI*440*t)|0.11*sin(2*PI*997*t):s=48000:d=12:c=stereo"),
        "stereo_44100": ("aevalsrc=0.2*sin(2*PI*440*t)|0.11*sin(2*PI*997*t):s=44100:d=12:c=stereo"),
        "surround_5_1_lfe": (
            "aevalsrc=0.10*sin(2*PI*440*t)|0.08*sin(2*PI*660*t)|"
            "0.12*sin(2*PI*880*t)|0.25*sin(2*PI*60*t)|"
            "0.07*sin(2*PI*330*t)|0.05*sin(2*PI*550*t):s=48000:d=12:c=5.1"
        ),
        "surround_7_1": (
            "aevalsrc=0.10*sin(2*PI*440*t)|0.08*sin(2*PI*660*t)|"
            "0.12*sin(2*PI*880*t)|0.25*sin(2*PI*60*t)|"
            "0.07*sin(2*PI*330*t)|0.05*sin(2*PI*550*t)|"
            "0.04*sin(2*PI*220*t)|0.03*sin(2*PI*1100*t):s=48000:d=12:c=7.1"
        ),
        "surround_16ch": (
            "aevalsrc="
            + "|".join(f"{0.03 + i * 0.002:.3f}*sin(2*PI*{130 + i * 37}*t)" for i in range(16))
            + ":s=48000:d=12:c=hexadecagonal"
        ),
        # Put impulses on 100 ms, 400 ms, and 3 s boundaries and repeat each
        # three-second cycle long enough to exercise circular history wraps.
        "impulses_boundaries_wrap": (
            "aevalsrc='if(eq(mod(n,144000),4800),1,if(eq(mod(n,144000),19200),0.75,"
            "if(eq(mod(n,144000),0),0.5,0)))':s=48000:d=60"
        ),
        "bench_stereo": (f"aevalsrc=0.18*sin(2*PI*440*t)|0.12*sin(2*PI*997*t):s=48000:d={long_seconds}:c=stereo"),
        "bench_5_1": (
            f"aevalsrc=0.10*sin(2*PI*440*t)|0.08*sin(2*PI*660*t)|"
            f"0.12*sin(2*PI*880*t)|0.25*sin(2*PI*60*t)|"
            f"0.07*sin(2*PI*330*t)|0.05*sin(2*PI*550*t):"
            f"s=48000:d={long_seconds}:c=5.1"
        ),
    }

    for name, source in sources.items():
        path = work / f"{name}.wav"
        make_lavfi(ffmpeg, path, source)
        cases.append(Case(name, path))

    codec_specs = [
        ("aac", "aac", "aac", False),
        ("ac3", "ac3", "ac3", False),
        ("eac3", "eac3", "eac3", False),
        ("flac", "flac", "flac", False),
        ("opus", "libopus", "opus", False),
        ("dts_core", "dca", "dts", True),
        ("truehd", "truehd", "truehd", True),
    ]
    codec_cases: list[Case] = []
    for name, encoder, codec, optional in codec_specs:
        path = work / f"encoded_{name}.mka"
        encode_command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(work / "surround_5_1_lfe.wav"),
            "-c:a",
            encoder,
            "-strict",
            "-2",
            "-f",
            "matroska",
            str(path),
        ]
        try:
            run(encode_command)
        except RuntimeError as exc:
            if optional:
                print(f"SKIP fixture codec_{name}: host FFmpeg lacks usable {encoder} encoder")
                continue
            raise RuntimeError(f"Host FFmpeg cannot prepare required {name} parity fixture: {exc}") from exc
        case = Case(f"codec_{name}", path, codec=codec)
        cases.append(case)
        codec_cases.append(case)

    # A real container with distinct audio streams verifies stream-index and
    # per-codec analysis. The input files above are generated fixtures only.
    multitrack = work / "multitrack.mkv"
    command = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    for case in codec_cases:
        command += ["-i", str(case.path)]
    for index in range(len(codec_cases)):
        command += ["-map", f"{index}:a:0"]
    command += ["-c", "copy", str(multitrack)]
    run(command)
    for index, case in enumerate(codec_cases):
        cases.append(Case(f"multitrack_{case.name.removeprefix('codec_')}", multitrack, index, case.codec))
    return cases


def report(binary: str, case: Case) -> dict[str, Any]:
    command = [binary, "-hide_banner", "-nostats"]
    if case.codec.lower() == "eac3":
        command += ["-drc_scale", "0"]
    command += ["-i", str(case.path), "-map", f"0:{case.stream}", "-af", FILTER, "-f", "null", "-"]
    stderr = run(command).stderr or ""
    starts = list(JSON_START.finditer(stderr))
    if not starts:
        raise RuntimeError(f"{binary} printed no loudnorm JSON for {case.name}:\n{stderr[-3000:]}")
    start = starts[-1].start()
    end = stderr.rfind("}")
    if end < start:
        raise RuntimeError(f"{binary} printed an incomplete loudnorm JSON for {case.name}:\n{stderr[-3000:]}")
    parsed = json.loads(stderr[start : end + 1], parse_constant=lambda value: f"<{value}>")
    missing = REQUIRED_PLEX_FIELDS - parsed.keys()
    if missing:
        raise RuntimeError(f"{binary} report for {case.name} lacks Plex loudness fields: {sorted(missing)}")
    return parsed


def analyze_command(binary: str, case: Case) -> list[str]:
    args = [binary, "-hide_banner", "-nostats"]
    if case.codec.lower() == "eac3":
        args += ["-drc_scale", "0"]
    return args + ["-i", str(case.path), "-map", f"0:{case.stream}", "-af", FILTER, "-f", "null", "-"]


def compare(reference: str, candidate: str, cases: list[Case]) -> bool:
    failures = 0
    for case in cases:
        ref = report(reference, case)
        got = report(candidate, case)
        if ref == got:
            print(f"PASS parity {case.name}: full JSON exact; all five Plex measurement fields present")
        else:
            failures += 1
            keys = sorted(set(ref) | set(got))
            differing = {key: (ref.get(key), got.get(key)) for key in keys if ref.get(key) != got.get(key)}
            print(f"FAIL parity {case.name}: {json.dumps(differing, sort_keys=True)}")
    return failures == 0


def benchmark(reference: str, candidate: str, cases: list[Case], repeats: int) -> None:
    print("Timing loudnorm runs (one warmup, alternating order):")
    measurements: dict[str, list[float]] = {"reference": [], "candidate": []}
    binaries = {"reference": reference, "candidate": candidate}
    for case in cases:
        for label in binaries:
            run(analyze_command(binaries[label], case))
        for trial in range(repeats):
            order = ("reference", "candidate") if trial % 2 == 0 else ("candidate", "reference")
            for label in order:
                start = time.perf_counter()
                run(analyze_command(binaries[label], case))
                elapsed = time.perf_counter() - start
                measurements[label].append(elapsed)
                print(f"  {case.name} run {trial + 1}/{repeats} {label}: {elapsed:.3f}s")
        ref = [measurements["reference"][-repeats + i] for i in range(repeats)]
        got = [measurements["candidate"][-repeats + i] for i in range(repeats)]
        ratio = statistics.median(ref) / statistics.median(got)
        print(
            f"  {case.name} median: reference={statistics.median(ref):.3f}s "
            f"candidate={statistics.median(got):.3f}s speedup={ratio:.3f}x"
        )


def resolve_binary(path: str, label: str) -> str:
    resolved = shutil.which(path) if os.path.sep not in path else path
    if not resolved or not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
        raise ValueError(f"{label} must name an executable FFmpeg binary: {path}")
    return os.path.realpath(resolved)


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reference", required=True, help="stock/reference FFmpeg executable")
    parser.add_argument("--candidate", required=True, help="patched/candidate FFmpeg executable")
    parser.add_argument(
        "--fixture-ffmpeg", default="ffmpeg", help="host FFmpeg used only to synthesize/encode fixtures"
    )
    parser.add_argument(
        "--long-seconds", type=int, default=60, help="duration of each stereo and 5.1 timing input (60-180)"
    )
    parser.add_argument("--repeats", type=int, default=3, help="measured timing runs per binary (after warmup)")
    parser.add_argument("--parity-only", action="store_true", help="compare reports without running long timing trials")
    args = parser.parse_args()
    if not 60 <= args.long_seconds <= 180:
        parser.error("--long-seconds must be between 60 and 180")
    if args.repeats < 1:
        parser.error("--repeats must be positive")

    reference = resolve_binary(args.reference, "--reference")
    candidate = resolve_binary(args.candidate, "--candidate")
    reference_sha256 = sha256_file(reference)
    candidate_sha256 = sha256_file(candidate)
    if reference_sha256 == candidate_sha256:
        parser.error("--reference and --candidate must have different binary contents")
    print(f"Reference: {reference} sha256={reference_sha256}")
    print(f"Candidate: {candidate} sha256={candidate_sha256}")
    fixture_ffmpeg = shutil.which(args.fixture_ffmpeg)
    if not fixture_ffmpeg:
        parser.error(f"fixture FFmpeg not found: {args.fixture_ffmpeg}")

    with tempfile.TemporaryDirectory(prefix="ffmpeg-loudness-verify-") as temporary:
        work = Path(temporary)
        print(f"Fixture directory: {work}")
        cases = make_fixtures(fixture_ffmpeg, work, args.long_seconds)
        parity_cases = [case for case in cases if not case.name.startswith("bench_")]
        timing_cases = [case for case in cases if case.name.startswith("bench_")]
        print(f"Comparing {len(parity_cases)} report fixtures")
        parity_ok = compare(reference, candidate, parity_cases)
        if not args.parity_only:
            benchmark(reference, candidate, timing_cases, args.repeats)
    return 0 if parity_ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
