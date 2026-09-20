"""What the AniSkip sweep returned, and how it scores against the files' own chapters.

Uses the app's own ``chapter_candidates()`` so the truth is the chapter the app would actually take,
cold-open rule and all -- including the episode kind, which the file's own path gives
(``ids_from_path``) exactly as the app derives it. That kind is what decides whether a bare
``Ending`` chapter is credits (phase 4, Task 15), and every file here is an anime episode, so
leaving it out measured a classifier this population never meets. Prints every number
``eval/aniskip-facts.md`` §1.4, §4 and §5 report.
"""

from __future__ import annotations

import gzip
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[5]))

from media_preview_generator.markers.external_ids import ids_from_path  # noqa: E402
from media_preview_generator.markers.models import MarkerType  # noqa: E402
from media_preview_generator.markers.probe import Chapter, MediaProbe  # noqa: E402
from media_preview_generator.markers.sources.chapters import chapter_candidates  # noqa: E402

# AniSkip has no preview type; a plain type wins over its "mixed" variant, which is a different answer.
PREFERENCE = {"intro": ("op", "mixed-op"), "credits": ("ed", "mixed-ed"), "recap": ("recap",)}
OUR_TYPE = {"op": "intro", "mixed-op": "intro", "ed": "credits", "mixed-ed": "credits", "recap": "recap"}
GENERIC_INTRO = re.compile(r"^(intro|introduction)$", re.I)
ENDING_CHAPTER = re.compile(r"^(ending|end)$", re.I)


def load() -> tuple[list[dict], dict[str, dict]]:
    """The sweep rows and the chapter probe, keyed by file."""
    sweep = []
    for line in (HERE / "aniskip_sweep.jsonl").open():
        row = json.loads(line)
        if "error" not in row:
            sweep.append(row)
    chapters = {c["file"]: c for c in json.loads((HERE / "anime_chapters.json").read_text()) if "err" not in c}
    return sweep, chapters


def answer(row: dict) -> dict[str, tuple[float, float, str, float]]:
    """AniSkip's one answer per our type: (start, end, which skipType, that entry's episodeLength)."""
    results = {x["skipType"]: x for x in (row.get("body") or {}).get("results") or []}
    out = {}
    for kind, keys in PREFERENCE.items():
        for key in keys:
            if key in results:
                hit = results[key]
                out[kind] = (hit["interval"]["startTime"], hit["interval"]["endTime"], key, hit["episodeLength"])
                break
    return out


def sanity_failure(kind: str, start: float, end: float, duration: float) -> str | None:
    """Why a segment fails spec §5.5 rule 2, or None."""
    if not duration:
        return "no duration"
    if end > duration + 2:
        return "ends past the file"
    if end - start < 3:
        return "shorter than 3 s"
    if kind in ("intro", "recap"):
        if start > duration * 0.35:
            return "intro/recap starts after 35%"
        if end - start > 300:
            return "intro/recap longer than 300 s"
        if end >= duration - 2:
            return "intro/recap runs to the end"
    elif kind == "credits" and start < duration * 0.75:
        return "credits start before the last 25%"
    return None


def report_sweep(sweep: list[dict]) -> None:
    """§4: what came back, the sanity failures and how close the matched cut was."""
    print(f"episodes asked: {len(sweep)}")
    print("HTTP status:", dict(Counter(r.get("status") for r in sweep)))
    by_type, per_episode, sanity, band = Counter(), Counter(), Counter(), Counter()
    both_edges = 0
    for row in sweep:
        duration = (row.get("duration_ms") or 0) / 1000
        kinds = set()
        for seg in (row.get("body") or {}).get("results") or []:
            start, end = seg["interval"]["startTime"], seg["interval"]["endTime"]
            both_edges += start is not None and end is not None
            by_type[seg["skipType"]] += 1
            kind = OUR_TYPE[seg["skipType"]]
            kinds.add(kind)
            sanity[sanity_failure(kind, start, end, duration) or "passes"] += 1
            delta = abs(duration - seg.get("episodeLength", 0))
            band["within 2 s" if delta <= 2 else "2-20 s away"] += 1
        for kind in kinds:
            per_episode[kind] += 1
        per_episode["any"] += bool(kinds)
    total = sum(by_type.values())
    print(f"segments returned: {total} (every one carried both edges: {both_edges == total})", dict(by_type))
    for kind in ("any", "intro", "credits", "recap"):
        print(f"  episodes with a {kind}: {per_episode[kind]} ({per_episode[kind] / len(sweep):.1%})")
    failed = total - sanity["passes"]
    print(f"spec §5.5 rule 2 over every segment: {dict(sanity)} -> {failed} of {total} fail ({failed / total:.1%})")
    print("matched cut vs the file's duration:", dict(band))

    disagree = Counter()
    for row in sweep:
        seen = {x["skipType"]: x["interval"] for x in (row.get("body") or {}).get("results") or []}
        for plain, mixed in (("op", "mixed-op"), ("ed", "mixed-ed")):
            if plain in seen and mixed in seen:
                gap = abs(seen[plain]["startTime"] - seen[mixed]["startTime"])
                disagree[f"{plain} vs {mixed}: {'<= 5 s' if gap <= 5 else '> 5 s'} apart"] += 1
    print("plain vs mixed on the same episode:", dict(disagree))


def chapter_truth(chapters: dict[str, dict], sweep_by_file: dict[str, dict]) -> tuple[dict, Counter]:
    """The intro/credits the app itself would take from each file's chapters."""
    truth, kinds = {}, Counter()
    for path, probe_row in chapters.items():
        duration_ms = (sweep_by_file.get(path) or {}).get("duration_ms")
        probe = MediaProbe(
            duration_ms, tuple(Chapter(c["start_ms"], c["end_ms"], c["name"]) for c in probe_row["chapters"])
        )
        candidates = chapter_candidates(probe, is_episode=ids_from_path(path).is_episode)
        intro = [c for c in candidates if c.type is MarkerType.INTRO]
        credits = [c for c in candidates if c.type is MarkerType.CREDITS]
        entry = {}
        if intro:
            best = min(intro, key=lambda c: c.end_ms if c.end_ms is not None else 1 << 40)
            entry["intro"] = (best.start_ms / 1000, (best.end_ms or 0) / 1000)
            entry["intro_origin"] = best.origin
            kinds["lone generic Intro" if GENERIC_INTRO.match(best.origin or "") else "specific opening"] += 1
        if credits:
            best = max(credits, key=lambda c: c.start_ms)
            entry["credits"] = (best.start_ms / 1000, (best.end_ms or 0) / 1000)
        if entry:
            truth[path] = entry
    return truth, kinds


def score(truth: dict, sweep_by_file: dict[str, dict], specific_opening_only: bool) -> None:
    """§5: intro judged on its end, credits on their start — the edges spec §5.5 rule 4 compares."""
    label = "specific opening chapters only" if specific_opening_only else "every chapter truth"
    print(f"\n--- scored against {label} ---")
    for kind in ("intro", "credits"):
        verdicts, tight, deltas = Counter(), Counter(), []
        for path, want in truth.items():
            if kind not in want:
                continue
            if specific_opening_only and kind == "intro" and GENERIC_INTRO.match(want.get("intro_origin") or ""):
                continue
            row = sweep_by_file.get(path)
            if row is None:
                continue
            got = answer(row).get(kind)
            if got is None:
                verdicts["missed"] += 1
                continue
            start, end, _which, episode_length = got
            delta = (end - want[kind][1]) if kind == "intro" else (start - want[kind][0])
            deltas.append(abs(delta))
            verdict = "useful <=5 s" if abs(delta) <= 5 else "off 5-15 s" if abs(delta) <= 15 else "wrong >15 s"
            verdicts[verdict] += 1
            if abs((row.get("duration_ms") or 0) / 1000 - episode_length) <= 2:
                tight[verdict] += 1
        deltas.sort()
        median = f"{deltas[len(deltas) // 2]:.1f} s" if deltas else "-"
        print(f"  {kind}: n={sum(verdicts.values())} {dict(verdicts)} | median |delta| {median}")
        print(f"     of those whose matched cut is within 2 s: {dict(tight)}")


def ending_chapters(chapters: dict[str, dict], sweep_by_file: dict[str, dict]) -> None:
    """Is a chapter named "Ending" the ED? The app's classifier says no; AniSkip is asked."""
    counts, deltas = Counter(), []
    for path, probe_row in chapters.items():
        named = [c for c in probe_row["chapters"] if ENDING_CHAPTER.match((c["name"] or "").strip())]
        if not named:
            continue
        counts["files with an Ending/End chapter"] += 1
        got = answer(sweep_by_file.get(path) or {}).get("credits")
        if got is None:
            counts["no AniSkip ed to compare"] += 1
            continue
        delta = min(abs(got[0] - c["start_ms"] / 1000) for c in named)
        deltas.append(delta)
        counts["<= 1 s"] += delta <= 1
        counts["<= 5 s"] += delta <= 5
        counts["> 15 s"] += delta > 15
    deltas.sort()
    median = f"{deltas[len(deltas) // 2]:.1f} s" if deltas else "-"
    print(f"\nAniSkip 'ed' vs a chapter named Ending/End: {dict(counts)} | compared {len(deltas)}, median {median}")


def ed_is_not_preview(chapters: dict[str, dict], sweep_by_file: dict[str, dict]) -> None:
    """§1.4: on files with both a credits and a preview chapter, which one is AniSkip's `ed`?

    The *last* credits chapter and the *first* preview chapter are used. Rule 3 takes the last of each; taking the
    first preview deliberately errs towards calling `ed` a preview, so the "0 within 5 s of the preview" result is
    the conservative one.
    """
    counts = Counter()
    for path, probe_row in chapters.items():
        row = sweep_by_file.get(path)
        got = answer(row or {}).get("credits")
        if got is None:
            continue
        probe = MediaProbe(
            (row or {}).get("duration_ms"),
            tuple(Chapter(c["start_ms"], c["end_ms"], c["name"]) for c in probe_row["chapters"]),
        )
        candidates = chapter_candidates(probe, is_episode=ids_from_path(path).is_episode)
        credits = [c for c in candidates if c.type is MarkerType.CREDITS]
        preview = [c for c in candidates if c.type is MarkerType.PREVIEW]
        if not (credits and preview):
            continue
        to_credits = abs(got[0] - max(c.start_ms for c in credits) / 1000)
        to_preview = abs(got[0] - min(c.start_ms for c in preview) / 1000)
        counts["nearer the credits chapter" if to_credits < to_preview else "nearer the preview chapter"] += 1
        counts["within 5 s of the credits chapter"] += to_credits <= 5
        counts["within 5 s of the preview chapter"] += to_preview <= 5
    print("\nAniSkip 'ed' start, on files with both a credits and a preview chapter:", dict(counts))


def index_size() -> None:
    """§3: how big the shipped tvdb -> (season, offset, mal) map would actually be."""
    fribb = json.loads((HERE / "fribb-full.json").read_text())
    anidb_to_mal = {int(e["anidb_id"]): int(e["mal_id"]) for e in fribb if e.get("anidb_id") and e.get("mal_id")}
    index = defaultdict(list)
    for anime in ET.parse(HERE / "animelists.xml").getroot().findall("anime"):
        tvdb, anidb = anime.get("tvdbid") or "", anime.get("anidbid")
        mal = anidb_to_mal.get(int(anidb)) if anidb else None
        if not tvdb.isdigit() or mal is None:
            continue
        try:
            offset = int(anime.get("episodeoffset") or "0")
        except ValueError:
            offset = 0
        index[tvdb].append([anime.get("defaulttvdbseason") or "", offset, mal])
    blob = json.dumps(index, separators=(",", ":")).encode()
    rules = sum(len(v) for v in index.values())
    print(
        f"\nshipped map: {len(index)} tvdb ids, {rules} rules, "
        f"{len(blob) / 1024:.0f} KB raw, {len(gzip.compress(blob)) / 1024:.0f} KB gzipped"
    )


def main() -> None:
    """Every §1.4 / §4 / §5 number in one run."""
    sweep, chapters = load()
    by_file = {r["file"]: r for r in sweep}
    report_sweep(sweep)
    truth, kinds = chapter_truth(chapters, by_file)
    print(f"\nfiles with a chapter truth: {len(truth)} | intro chapter kind: {dict(kinds)}")
    ed_is_not_preview(chapters, by_file)
    score(truth, by_file, specific_opening_only=False)
    score(truth, by_file, specific_opening_only=True)
    ending_chapters(chapters, by_file)
    index_size()


if __name__ == "__main__":
    main()
