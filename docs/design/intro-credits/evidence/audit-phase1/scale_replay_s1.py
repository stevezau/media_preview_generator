#!/usr/bin/env python3
"""Scale-run fix S1 (findings F2, F3): replay decide() on the lab scale run's stored evidence, before and after.

    MPG_REPO=<checkout> ./scale_replay_s1.py run <label>   decide() of that checkout -> results/scale/replay-s1-<label>.json
    ./scale_replay_s1.py compare <before> <after>          aggregates -> results/scale/replay-s1-compare.json (+ stdout)

Evidence is the backfill job's stored evidence (`collected-backfill.json`). Prod Plex's own markers
(`prod_plex_markers.json`, a read-only DB dump) are injected as server-marker candidates the way the app's reader would
see them: served times (credits start +2 s, non-final end -2 s, final end = end of file), and nothing for an item whose
parts aren't all this file's cut (2 s). Each file is decided at High and Medium, with and without the prod markers.
Files are matched to prod by the truth file's host path, else by a unique basename.

Everything under results/ is git-ignored (it lists library paths): only aggregate numbers and show/movie names may
leave it. `MPG_EVIDENCE` points at the evidence folder holding lab/results/ (a worktree has no results).
"""

from __future__ import annotations

import importlib.util
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = os.environ.get("MPG_REPO") or str(HERE.parents[3])
EVIDENCE = Path(os.environ.get("MPG_EVIDENCE") or HERE.parent)
OUT = EVIDENCE / "lab" / "results" / "scale"
SOURCE_ORDER = (
    "chapters",
    "theintrodb",
    "introdb",
    "skipdb",
    "season_audio",
    "credits_text",
    "server_markers",
    "server_markers_imported",
)
PROD_ORIGIN = "prod-plex"
SAME_CUT_MS = 2_000
CREDITS_SERVE_SHIFT_MS = 2_000
VARIANTS = ("high", "medium", "high_noinject", "medium_noinject")
SHORTENED = "shortened to the server's own marker"
NAMED = ("Avatar (2009)", "Innerspace (1987)")


def _load(name: str):
    return json.loads((OUT / name).read_text())


def _scale_score():
    """The lab scale run's own grading (grade, content_after_credits, show_of), so numbers compare with its report."""
    spec = importlib.util.spec_from_file_location("scale_score", EVIDENCE / "lab" / "scale_score.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prod_index() -> tuple[dict, dict, dict]:
    rows_by_file: dict[str, list[dict]] = defaultdict(list)
    for row in _load("prod_plex_markers.json"):
        rows_by_file[row["file"]].append(row)
    durations_by_item: dict[int, list[int | None]] = defaultdict(list)
    item_by_file: dict[str, int] = {}
    files_by_name: dict[str, set[str]] = defaultdict(set)
    for part in _load("prod_plex_parts.json"):
        durations_by_item[part["item"]].append(part["duration"])
        item_by_file[part["file"]] = part["item"]
        files_by_name[os.path.basename(part["file"])].add(part["file"])
    return rows_by_file, {"durations": durations_by_item, "item": item_by_file}, files_by_name


def _prod_file(entry: dict, truth: dict, parts: dict, files_by_name: dict) -> str | None:
    host = (truth.get(entry["file"]) or {}).get("host")
    if host in parts["item"]:
        return host
    named = files_by_name.get(os.path.basename(entry["file"])) or set()
    return next(iter(named)) if len(named) == 1 else None


def _one_cut(durations: list[int | None], duration_ms: int) -> bool:
    if len(durations) <= 1:
        return True
    return all(d is not None and abs(d - duration_ms) <= SAME_CUT_MS for d in durations)


def run(label: str) -> None:
    sys.path.insert(0, REPO)
    from media_preview_generator.markers.decide import DecisionContext, DecisionStatus, decide  # noqa: PLC0415
    from media_preview_generator.markers.models import Candidate, MarkerType, Source  # noqa: PLC0415

    truth = _load("truth.json")
    rows_by_file, parts, files_by_name = _prod_index()
    out = []
    matched = Counter()
    for entry in _load("collected-backfill.json"):
        if not entry.get("decisions"):
            continue
        stored = [
            Candidate(
                MarkerType(e["type"]),
                e["start_ms"],
                e["end_ms"],
                Source(e["source"]),
                1.0 if e["confidence"] is None else e["confidence"],
                e.get("origin") or "",
            )
            for e in entry.get("evidence") or []
            if e.get("type")
        ]
        prod = _prod_file(entry, truth, parts, files_by_name)
        injected = []
        if prod is None:
            matched["no prod file"] += 1
        elif not _one_cut(parts["durations"][parts["item"][prod]], entry["duration_ms"] or 0):
            matched["prod item has another cut (not read)"] += 1
        else:
            matched["prod file matched"] += 1
            for row in rows_by_file.get(prod, []):
                mtype = MarkerType(row["type"])
                start, end = row["start"], row["end"]
                if mtype is MarkerType.CREDITS:
                    final = '"pv:final":"1"' in (row.get("extra") or "")
                    start, end = start + CREDITS_SERVE_SHIFT_MS, None if final else end - CREDITS_SERVE_SHIFT_MS
                injected.append(Candidate(mtype, start, end, Source.SERVER_MARKERS, 1.0, PROD_ORIGIN))
        types = frozenset({MarkerType.CREDITS} if entry["is_movie"] else {MarkerType.CREDITS, MarkerType.INTRO})
        decisions = {}
        for variant in VARIANTS:
            level = variant.split("_")[0]
            cands = stored if variant.endswith("noinject") else stored + injected
            ctx = DecisionContext(entry["duration_ms"] or 0, bool(entry["is_movie"]), level, types, SOURCE_ORDER)
            got = decide(cands, ctx, {})
            decisions[variant] = {
                t.value: {
                    "status": got[t].status.value,
                    "marker": (
                        [got[t].marker.start_ms, got[t].marker.end_ms, list(got[t].marker.decided_by)]
                        if got[t].status is DecisionStatus.DECIDED
                        else None
                    ),
                    "reason": got[t].reason,
                }
                for t in types
            }
        out.append({"file": entry["file"], "prod_markers": len(injected), "decisions": decisions})
    (OUT / f"replay-s1-{label}.json").write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(f"{label}: {len(out)} files, {dict(matched)}")


def _grade_key(scale, mtype: str, marker, t_entry: dict) -> str:
    t = t_entry["truth"].get(mtype)
    if not t:
        return "no truth"
    after = mtype == "credits" and scale.content_after_credits(t_entry, t["end"])
    return scale.grade(mtype, tuple(marker[:2]) if marker else None, t, after)


def _lone(decision: dict, source: str) -> bool:
    return bool(decision["marker"]) and decision["marker"][2] == [source]


def compare(before_label: str, after_label: str) -> dict:
    scale = _scale_score()
    truth = _load("truth.json")
    before = {e["file"]: e for e in _load(f"replay-s1-{before_label}.json")}
    after = {e["file"]: e for e in _load(f"replay-s1-{after_label}.json")}
    tallies: dict[str, Counter] = defaultdict(Counter)
    clamp_s: dict[str, list[float]] = defaultdict(list)
    shows: dict[str, Counter] = defaultdict(Counter)
    named = {}
    for path, b_entry in before.items():
        a_entry = after[path]
        t_entry = truth.get(path)
        show = scale.show_of(path)
        for variant in VARIANTS:
            for mtype, b in b_entry["decisions"][variant].items():
                a = a_entry["decisions"][variant][mtype]
                key = f"{variant} {mtype}"
                if (b["status"], b["marker"]) != (a["status"], a["marker"]):
                    if SHORTENED in a["reason"] and b["status"] == a["status"] == "decided":
                        basis = "chapters" if "chapters" in b["marker"][2] else "agreement/medium"
                        kind = f"shortened ({basis})"
                        edge = 0 if mtype == "credits" else 1
                        clamp_s[key].append(abs(a["marker"][edge] - b["marker"][edge]) / 1000)
                        if variant == "high":
                            shows[show][f"{mtype} shortened"] += 1
                    elif b["status"] == "decided" and a["status"] == "needs_review" and _lone(b, "skipdb"):
                        kind = "lone SkipDB -> needs review"
                    else:
                        kind = f"other: {b['status']} -> {a['status']} ({a['reason']})"
                    tallies[f"changed: {key}"][kind] += 1
                if t_entry:
                    tallies[f"truth {key} (before)"][_grade_key(scale, mtype, b["marker"], t_entry)] += 1
                    tallies[f"truth {key} (after)"][_grade_key(scale, mtype, a["marker"], t_entry)] += 1
                if mtype == "credits" and variant.startswith("medium"):
                    high = b_entry["decisions"][variant.replace("medium", "high")]["credits"]
                    if _lone(b, "skipdb") and not high["marker"]:
                        tallies[f"lone SkipDB credits added by {variant}"][f"after: {a['status']}"] += 1
        if any(n in path for n in NAMED):
            name = next(n for n in NAMED if n in path)
            named[name] = {
                variant: {
                    "before": before[path]["decisions"][variant]["credits"],
                    "after": a_entry["decisions"][variant]["credits"],
                    "grade before": _grade_key(
                        scale, "credits", b_entry["decisions"][variant]["credits"]["marker"], t_entry
                    )
                    if t_entry
                    else None,
                    "grade after": _grade_key(
                        scale, "credits", a_entry["decisions"][variant]["credits"]["marker"], t_entry
                    )
                    if t_entry
                    else None,
                }
                for variant in ("high", "high_noinject")
            }
    result = {
        "tallies": {k: dict(sorted(v.items())) for k, v in sorted(tallies.items())},
        "shortened seconds": {
            k: {"count": len(v), "median": statistics.median(v), "max": max(v)} for k, v in sorted(clamp_s.items())
        },
        "shows with shortened markers (high)": {k: dict(v) for k, v in sorted(shows.items())},
        "named": named,
    }
    (OUT / "replay-s1-compare.json").write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps(result, indent=1, ensure_ascii=False))
    return result


def main(argv: list[str]) -> int:
    if argv[:1] == ["run"] and len(argv) == 2:
        run(argv[1])
        return 0
    if argv[:1] == ["compare"] and len(argv) == 3:
        compare(argv[1], argv[2])
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
