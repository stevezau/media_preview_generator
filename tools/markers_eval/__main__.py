"""python -m tools.markers_eval <command>: see tools/markers_eval/README.md."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from media_preview_generator.markers.audio import end_picture
from media_preview_generator.markers.audio.fingerprint import chromaprint_ffmpeg
from media_preview_generator.markers.probe import ffprobe_path_for

from .cache import FingerprintCache
from .data import evidence_dir, load_v3_results
from .intros import SPEC_SEASON_STEP, SPEC_V3, DecodedEndPictures, reproduce, season_truth
from .plex import export_sql
from .report import DEFAULT_BASELINE, full_report


def _cache(args: argparse.Namespace) -> FingerprintCache:
    ffmpeg = chromaprint_ffmpeg(args.ffmpeg)
    if ffmpeg is None:
        sys.exit("No ffmpeg with chromaprint found (pass --ffmpeg)")
    root = Path(args.cache or os.environ.get("MARKERS_EVAL_CACHE") or Path.home() / ".cache/markers_eval")
    return FingerprintCache(root, ffmpeg=ffmpeg, ffprobe=ffprobe_path_for(ffmpeg))


def _end_pictures(args: argparse.Namespace) -> DecodedEndPictures:
    """The season step's end-picture check on the real files, decoded like a worker would (``--decode``)."""
    ffmpeg = chromaprint_ffmpeg(args.ffmpeg) or "ffmpeg"
    gpu = "NVIDIA" if args.decode == "gpu" else None
    reader = end_picture.Reader(ffmpeg=ffmpeg, gpu=gpu, gpu_device_path=args.gpu_device if gpu else None)
    return DecodedEndPictures(reader)


def cmd_reproduce(args: argparse.Namespace) -> int:
    report = reproduce(
        load_v3_results(),
        points=_cache(args).points,
        end_pictures=_end_pictures(args),
        with_reference=not args.no_reference,
        full_folder=args.full_folder,
    )
    summary = {
        "mode": "full folder" if args.full_folder else "eval lists",
        "seasons": report.seasons,
        "episodes": report.episodes,
        "tally": report.tally.as_dict(),
        "matcher_tally": report.matcher_tally.as_dict(),
        "spec": dict(zip(("useful", "wrong", "missed"), SPEC_SEASON_STEP, strict=True)),
        "matcher_spec": dict(zip(("useful", "wrong", "missed"), SPEC_V3, strict=True)),
        # Without the reference that check never ran: 0 would read as "the port matches it".
        "port_vs_reference": "not checked" if args.no_reference else len(report.port_vs_reference),
        "drift": len(report.drift),
        "skipped_pairs": len(report.skipped_pairs),
        "silence_dropped": len(report.silence_dropped),
        "guards_changed": len(report.guards_changed),
        "passed": report.passed,
    }
    print(json.dumps(summary, indent=2))
    if args.json:
        details = {"port_vs_reference": report.port_vs_reference, "drift": report.drift,
                   "skipped_pairs": report.skipped_pairs, "silence_dropped": report.silence_dropped,
                   "guards_changed": report.guards_changed}  # fmt: skip
        Path(args.json).write_text(json.dumps({**summary, "details": details}, indent=1, default=str))
    return 0 if report.passed else 1


def cmd_season_truth(args: argparse.Namespace) -> int:
    raw = json.loads(Path(args.truth).read_text())
    truth = {path: (float(value[0]), float(value[1])) if value else None for path, value in raw.items()}
    report = season_truth(truth, points=_cache(args).points, end_pictures=_end_pictures(args))
    summary = {"files": len(truth), "tally": report.tally.as_dict(), "none_ok": report.none_ok}
    passed = True
    if args.expect:
        useful, wrong = (int(n) for n in args.expect.split(","))
        summary["expect"] = {"useful": useful, "wrong": wrong}
        passed = report.tally.at_least(useful, wrong)
    summary["passed"] = passed
    print(json.dumps(summary, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps({**summary, "details": report.details}, indent=1, default=str))
    return 0 if passed else 1


def _decode_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--decode", choices=("gpu", "cpu"), default="gpu", help="the end-picture check's decode")
    parser.add_argument("--gpu-device", default="cuda:0")


def cmd_plex_sql(args: argparse.Namespace) -> int:
    folders = {e.season for e in load_v3_results()}
    evidence = evidence_dir()
    for name in ("movies40", "tv40", "movie_credit_truth"):
        folders |= {os.path.dirname(r["file"]) for r in json.loads((evidence / f"credits/{name}.json").read_text())}
    print(export_sql(sorted(folders)), end="")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    fingerprints = _cache(args)
    baseline = Path(args.plex_baseline) if args.plex_baseline else evidence_dir() / DEFAULT_BASELINE
    summary, details, passed = full_report(
        fingerprints, ffprobe=ffprobe_path_for(chromaprint_ffmpeg(args.ffmpeg)), baseline_path=baseline,
        full_folder=args.full_folder, end_pictures=_end_pictures(args),
    )  # fmt: skip
    print(json.dumps(summary, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps({**summary, "details": details}, indent=1, default=str))
    return 0 if passed else 1


def cmd_credits_text(args: argparse.Namespace) -> int:
    from .credits_text import SweepError, UnknownSetError, run_credits_text, sweep_credits_text

    ffmpeg = args.ffmpeg or shutil.which("ffmpeg")
    if ffmpeg is None:
        sys.exit("No ffmpeg found (pass --ffmpeg)")
    root = Path(args.cache or os.environ.get("MARKERS_EVAL_CACHE") or Path.home() / ".cache/markers_eval")
    baseline = Path(args.plex_baseline) if args.plex_baseline else evidence_dir() / DEFAULT_BASELINE
    if args.sweep:
        # A sweep reports its cells, not one run: it has no single answer set to diff, no online cases and no sheets,
        # so these would be accepted and then quietly dropped.
        ignored = [name for name in ("changed_since", "online", "sheets") if getattr(args, name)]
        if ignored:
            sys.exit("--sweep cannot be combined with " + ", ".join(f"--{name.replace('_', '-')}" for name in ignored))
        try:
            summary, _cells = sweep_credits_text(
                decode=args.decode, gpu_device=args.gpu_device, sets=tuple(args.sets.split(",")), specs=args.sweep,
                cache_root=root, ffmpeg=ffmpeg, ffprobe=ffprobe_path_for(ffmpeg), baseline_path=baseline,
            )  # fmt: skip
        except (SweepError, UnknownSetError) as exc:
            # A sweep that measured nothing must never print a table: both of these mean no cell was run as asked.
            sys.exit(str(exc))
        table = summary.pop("table")
        print(table)
        print(json.dumps(summary, indent=2))
        if args.json:
            Path(args.json).write_text(json.dumps({**summary, "table": table}, indent=1, default=str))
        return 0
    before = json.loads(Path(args.changed_since).read_text())["details"] if args.changed_since else None
    try:
        summary, details, passed = run_credits_text(
            decode=args.decode, gpu_device=args.gpu_device, sets=tuple(args.sets.split(",")), online=args.online,
            cache_root=root, ffmpeg=ffmpeg, ffprobe=ffprobe_path_for(ffmpeg), baseline_path=baseline,
            sheets_dir=Path(args.sheets) if args.sheets else None, before=before,
        )  # fmt: skip
    except UnknownSetError as exc:
        # A set that never ran must never look like a set that passed. Only this error: any other one (a corrupt
        # cache or evidence file is a JSONDecodeError, itself a ValueError) keeps its traceback.
        sys.exit(str(exc))
    print(json.dumps(summary, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps({**summary, "details": details}, indent=1, default=str))
    return 0 if passed else 1


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
    _decode_arguments(rep)
    rep.set_defaults(func=cmd_reproduce)
    truth = sub.add_parser("season-truth", help="the app's season step on an intro truth file (e.g. the Accused set)")
    truth.add_argument("--truth", required=True, help='JSON {"<file>": [start_s, end_s] or null} (local-only)')
    truth.add_argument("--expect", help="useful,wrong: exit 1 below that many useful or above that many wrong")
    truth.add_argument("--ffmpeg")
    truth.add_argument("--cache")
    truth.add_argument("--json", help="write details (local-only: holds file paths)")
    _decode_arguments(truth)
    truth.set_defaults(func=cmd_season_truth)
    sql = sub.add_parser("plex-sql", help="read-only SQL exporting prod Plex's markers for the eval files")
    sql.set_defaults(func=cmd_plex_sql)
    full = sub.add_parser("report", help="decisions vs Plex's own markers, online cases, credits chapter rules")
    full.add_argument("--ffmpeg")
    full.add_argument("--cache")
    full.add_argument("--plex-baseline", help=f"Plex's markers (default: evidence/{DEFAULT_BASELINE})")
    full.add_argument("--json", help="write details (local-only: holds file paths)")
    full.add_argument(
        "--full-folder", action="store_true", help="match each whole season folder (the app) instead of the eval lists"
    )
    _decode_arguments(full)
    full.set_defaults(func=cmd_report)
    text = sub.add_parser("credits-text", help="credit text (rule J) on the 80- and 205-file credits sets vs Plex")
    text.add_argument("--decode", choices=("gpu", "cpu"), default="gpu")
    text.add_argument("--gpu-device", default="cuda:0")
    text.add_argument("--sets", default="80,205", help="comma-separated: 80, 205")
    text.add_argument("--online", action="store_true", help="also the 43 verified online cases")
    text.add_argument(
        "--sheets",
        help="write frame-check sheets (answers >10 s early, >30 s late, epilogue-like, with an end) to this "
        "local-only folder",
    )
    text.add_argument(
        "--changed-since",
        help="an earlier run's --json file: list every answer that moved more than 10 s against it (and give each "
        "sheets with --sheets)",
    )
    text.add_argument(
        "--sweep",
        action="append",
        metavar="rule_j.NAME=v1,v2,...",
        help="sweep one of rule J's constants over these values instead of reporting a run (repeat for a cross "
        "product); prints a markdown table carrying the columns evidence/eval/phase3-harness.md's sweep "
        "tables carry, plus what each cell decoded",
    )
    text.add_argument("--ffmpeg")
    text.add_argument("--cache")
    text.add_argument("--plex-baseline", help=f"Plex's markers (default: evidence/{DEFAULT_BASELINE})")
    text.add_argument("--json", help="write details (local-only: holds file paths)")
    text.set_defaults(func=cmd_credits_text)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
