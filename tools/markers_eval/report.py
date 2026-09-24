"""The full report (``python -m tools.markers_eval report``): intro decisions vs Plex, online cases, credits."""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path

from media_preview_generator.markers.external_ids import ids_from_path

from .cache import FingerprintCache, ProbeCache
from .credits import CreditsReport, CreditsRows, chapter_rules, compare_credits_with_plex, title_coverage
from .data import EvalEpisode, evidence_dir, load_v3_results
from .decisions import audio_candidate, compare_with_plex, g3_differences, season_segments
from .intros import SPEC_V3, EndPictures
from .online import SETTINGS, case_file, case_key, load_online, online_verdicts, plex_online, tally
from .plex import PlexMarker, load_baseline, server_candidates

DEFAULT_BASELINE = "lab/results/scale/prod_plex_markers.json"
SCALE_TRUTH = "lab/results/scale/truth.json"
# Rick and Morty S01's truth is prod Plex's own markers (online/build_cases.py), so Plex can't be judged on it.
PLEX_TRUTH_SOURCE = "3-source agreement"


def _counts(counts: dict[str, Counter]) -> dict[str, dict[str, int]]:
    return {mtype: dict(sorted(c.items())) for mtype, c in counts.items()}


def _online(
    evidence: Path,
    baseline: dict[str, list[PlexMarker]],
    fingerprints: FingerprintCache,
    end_pictures: EndPictures,
) -> tuple[dict, dict]:
    results, dump = load_online(evidence)
    files = {case_key(r["case"]): case_file(r["case"], baseline.keys()) for r in results}
    found = {key: path for key, path in files.items() if path}
    cases = {case_key(r["case"]): r["case"] for r in results}
    episodes = [EvalEpisode(os.path.dirname(path), path, tuple(cases[key]["intro"]), None, None, cases[key]["dur"])
                for key, path in found.items()]  # fmt: skip
    segments = season_segments(episodes, points=fingerprints.points, full_folder=True, end_pictures=end_pictures)
    extra = {
        key: server_candidates(baseline[path]) + ([audio_candidate(segments[path])] if segments.get(path) else [])
        for key, path in found.items()
    }
    plex_by_case = {key: baseline[path] for key, path in found.items()}
    subsets = {
        "all": results,
        "truth_not_plex": [r for r in results if r["case"]["truth_src"] != PLEX_TRUTH_SOURCE],
    }
    summary: dict = {"cases": len(results), "files_found": len(found),
                     "season_audio_answers": sum(1 for s in segments.values() if s)}  # fmt: skip
    details: dict = {"files": {f"{k[0]} S{k[1]:02d}E{k[2]:02d}": v for k, v in files.items()}, "settings": {}}
    for subset, rows in subsets.items():
        block: dict = {"cases": len(rows), "plex": _counts(plex_online(rows, plex_by_case)), "settings": {}}
        for label, order, level in SETTINGS:
            only = online_verdicts(rows, dump, order=order, level=level)
            on = online_verdicts(rows, dump, order=order, level=level, extra=extra)
            off = online_verdicts(rows, dump, order=order, level=level, extra=extra, g3=False)
            block["settings"][label] = {
                "online_only": _counts(tally(only)),
                "with_plex_and_audio_g3_on": _counts(tally(on)),
                "with_plex_and_audio_g3_off": _counts(tally(off)),
                "g3_changed": sum(a["verdict"] != b["verdict"] for a, b in zip(on, off, strict=True)),
            }
            if subset == "all":
                details["settings"][label] = {"online_only": only, "g3_on": on, "g3_off": off}
        summary[subset] = block
    return summary, details


def _credits_rows(rows: CreditsRows) -> dict:
    return {
        "files": len(rows.files),
        "plex": dict(sorted(rows.plex.items())),
        "chapters": dict(sorted(rows.chapters.items())),
        "high": dict(sorted(rows.high.items())),
        "medium": dict(sorted(rows.medium.items())),
        "plex_has_credits": sum(1 for f in rows.files if f["plex"] is not None),
        "plex_several_credits_markers": sum(1 for f in rows.files if f["plex_markers"] > 1),
        "rule7_shortened": sum(1 for f in rows.files if f["shortened"]),
    }


def _scale_run_titles(evidence: Path) -> dict | None:
    """Ledger L165 on the lab scale run's chapters (the only stored set with anime): "Ending" titles, two credits.

    The set holds movies as well as episodes, and "Ending" is credits on an episode only (spec §5.1), so
    each row is scored with its own kind -- scoring the whole set as episodes would hide exactly the
    movie-side regression this ledger exists to surface.
    """
    path = evidence / SCALE_TRUTH
    if not path.exists():
        return None
    entries = [{"file": v["host"], "chapters": [c[0] for c in v.get("chapters") or []]}
               for v in json.loads(path.read_text()).values()]  # fmt: skip
    by_kind = {True: [], False: []}
    for entry in entries:
        by_kind[ids_from_path(entry["file"]).is_episode].append(entry)
    reports = {kind: title_coverage(rows, is_episode=kind) for kind, rows in by_kind.items()}
    return {"files": len(entries), "episodes": len(by_kind[True]), "movies": len(by_kind[False]),
            "with_credits_title": sum(r.tally["found"] for r in reports.values()),
            "ending_titles": sorted({n for r in reports.values() for n in r.ending_titles}),
            "several_credits_chapters": sorted({n for r in reports.values() for n in r.several_credits})}  # fmt: skip


def _credits(evidence: Path, baseline: dict[str, list[PlexMarker]], probes: ProbeCache) -> tuple[dict, dict]:
    def load(name: str) -> list[dict]:
        return json.loads((evidence / f"credits/{name}.json").read_text())

    movies40, tv40, set205 = load("movies40"), load("tv40"), load("movie_credit_truth")
    adjudicated = load("adjudicated")
    # Movies and episodes are scored separately because "Ending" only counts as credits on an episode.
    movie_rules = chapter_rules(movies40, adjudicated, probe=probes.probe, is_episode=False)
    tv_rules = chapter_rules(tv40, adjudicated, probe=probes.probe, is_episode=True)
    rules = CreditsReport(
        tally=movie_rules.tally + tv_rules.tally,
        titles_missed=movie_rules.titles_missed + tv_rules.titles_missed,
        several_credits=movie_rules.several_credits + tv_rules.several_credits,
        ending_titles=movie_rules.ending_titles + tv_rules.ending_titles,
    )
    coverage = title_coverage(set205)
    sets = {
        "movies40": compare_credits_with_plex(
            movies40, adjudicated, probe=probes.probe, baseline=baseline, is_movie=True
        ),
        "tv40": compare_credits_with_plex(tv40, adjudicated, probe=probes.probe, baseline=baseline, is_movie=False),
        "set205": compare_credits_with_plex(set205, adjudicated, probe=probes.probe, baseline=baseline, is_movie=True),
    }
    several = {f["name"] for rows in sets.values() for f in rows.files if f["credits_chapters"] > 1}
    ending = {f["name"] for rows in sets.values() for f in rows.files if f["ending_titles"]}
    summary = {
        "scale_run_titles": _scale_run_titles(evidence),
        "chapter_rules_80": dict(sorted(rules.tally.items())),
        **{name: _credits_rows(rows) for name, rows in sets.items()},
        "titles_205": dict(sorted(coverage.tally.items())),
        "several_credits_chapters": sorted(several | set(rules.several_credits) | set(coverage.several_credits)),
        "ending_titles": sorted(ending | set(rules.ending_titles) | set(coverage.ending_titles)),
        "titles_missed": sorted(set(coverage.titles_missed)),
    }
    return summary, {name: rows.files for name, rows in sets.items()}


def full_report(
    fingerprints: FingerprintCache,
    *,
    ffprobe: str,
    baseline_path: Path,
    full_folder: bool,
    end_pictures: EndPictures,
) -> tuple[dict, dict, bool]:
    """Run every part of the report.

    Args:
        fingerprints: The fingerprint cache (the probe cache lives next to it).
        ffprobe: ffprobe binary.
        baseline_path: Plex's markers (``plex.load_baseline``).
        full_folder: The 118-episode rows match whole season folders (the app) instead of the eval lists. Online cases
            always match whole folders.
        end_pictures: The season step's end-picture check (``intros.DecodedEndPictures`` on real files).

    Returns:
        The summary (counts and folder names only), the details (file paths: local-only), and whether the shipped
        rules pass the gate (Medium beats Plex and High is no more wrong, with G3 on) with season audio at spec §5.3.
    """
    evidence = evidence_dir()
    baseline = load_baseline(baseline_path)
    episodes = load_v3_results()
    segments = season_segments(episodes, points=fingerprints.points, full_folder=full_folder, end_pictures=end_pictures)
    on = compare_with_plex(episodes, segments, baseline)
    off = compare_with_plex(episodes, segments, baseline, g3=False)
    differences = g3_differences(on, off)
    online_summary, online_details = _online(evidence, baseline, fingerprints, end_pictures)
    credits_summary, credits_details = _credits(evidence, baseline, ProbeCache(fingerprints.root, ffprobe=ffprobe))
    summary = {
        "mode": "full folder" if full_folder else "eval lists",
        "intros": {
            "episodes": len(on.episodes),
            "plex_intro_markers": sum(1 for row in on.episodes if row["plex_segment"] is not None),
            "g3_on": {**on.as_dict(), "gate": on.gate()},
            "g3_off": {**off.as_dict(), "gate": off.gate()},
            "g3_changed": dict(sorted(Counter(d["g3_off"] for d in differences).items())),
        },
        "online": online_summary,
        "credits": credits_summary,
    }
    details = {
        "intros": {"g3_on": on.episodes, "g3_off": off.episodes, "g3_differences": differences},
        "online": online_details,
        "credits": credits_details,
    }
    return summary, details, on.gate() and on.audio.at_least(*SPEC_V3[:2])
