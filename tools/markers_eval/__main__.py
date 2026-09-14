"""python -m tools.markers_eval <command>: see tools/markers_eval/README.md."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from media_preview_generator.markers.audio.fingerprint import chromaprint_ffmpeg
from media_preview_generator.markers.probe import ffprobe_path_for

from .cache import FingerprintCache
from .data import load_v3_results
from .intros import SPEC_V3, reproduce


def _cache(args: argparse.Namespace) -> FingerprintCache:
    ffmpeg = chromaprint_ffmpeg(args.ffmpeg)
    if ffmpeg is None:
        sys.exit("No ffmpeg with chromaprint found (pass --ffmpeg)")
    root = Path(args.cache or os.environ.get("MARKERS_EVAL_CACHE") or Path.home() / ".cache/markers_eval")
    return FingerprintCache(root, ffmpeg=ffmpeg, ffprobe=ffprobe_path_for(ffmpeg))


def cmd_reproduce(args: argparse.Namespace) -> int:
    report = reproduce(
        load_v3_results(),
        points=_cache(args).points,
        with_reference=not args.no_reference,
        full_folder=args.full_folder,
    )
    summary = {
        "mode": "full folder" if args.full_folder else "eval lists",
        "seasons": report.seasons,
        "episodes": report.episodes,
        "tally": report.tally.as_dict(),
        "matcher_tally": report.matcher_tally.as_dict(),
        "spec": dict(zip(("useful", "wrong", "missed"), SPEC_V3, strict=True)),
        "port_vs_reference": len(report.port_vs_reference),
        "drift": len(report.drift),
        "skipped_pairs": len(report.skipped_pairs),
        "silence_dropped": len(report.silence_dropped),
        "passed": report.passed,
    }
    print(json.dumps(summary, indent=2))
    if args.json:
        details = {"port_vs_reference": report.port_vs_reference, "drift": report.drift,
                   "skipped_pairs": report.skipped_pairs, "silence_dropped": report.silence_dropped}  # fmt: skip
        Path(args.json).write_text(json.dumps({**summary, "details": details}, indent=1, default=str))
    return 0 if report.passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.markers_eval")
    sub = parser.add_subparsers(dest="command", required=True)
    rep = sub.add_parser("reproduce", help="v3 matcher port vs reference and spec §5.3 on the 118 episodes")
    rep.add_argument("--ffmpeg")
    rep.add_argument("--cache")
    rep.add_argument("--json", help="write details (local-only: holds file paths)")
    rep.add_argument("--no-reference", action="store_true", help="skip the slow pure-Python reference")
    rep.add_argument(
        "--full-folder", action="store_true", help="match each whole season folder (the app) instead of the eval lists"
    )
    rep.set_defaults(func=cmd_reproduce)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
