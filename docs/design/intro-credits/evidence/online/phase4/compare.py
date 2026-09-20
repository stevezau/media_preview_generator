"""AniSkip against the app's other sources, on a random sample of the same anime episodes.

The other online sources are asked through the app's **own** clients, chapters through its own
``chapter_candidates()``, and Plex's markers come from the phase-2 read-only dump — so this measures
the sources as the app sees them, not a second implementation.

The sample is drawn from every resolved episode, including the ones AniSkip answers 404 for, so the
coverage column is not biased towards AniSkip.
"""

from __future__ import annotations

import itertools
import json
import os
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[5]))

from loguru import logger  # noqa: E402

logger.remove()  # the clients log every lookup at DEBUG; this script prints its own summary

from media_preview_generator.markers.models import MarkerType, MediaIds  # noqa: E402
from media_preview_generator.markers.probe import Chapter, MediaProbe  # noqa: E402
from media_preview_generator.markers.sources import introdb, skipdb, theintrodb  # noqa: E402
from media_preview_generator.markers.sources.chapters import chapter_candidates  # noqa: E402

PLEX_MARKERS = Path(os.environ.get("MARKERS_PLEX_DUMP") or HERE / "../../lab/results/scale/prod_plex_markers.json")
SOURCES = ("aniskip", "theintrodb", "introdb", "skipdb", "chapters", "plex")
PREFERENCE = {"intro": ("op", "mixed-op"), "credits": ("ed", "mixed-ed"), "recap": ("recap",)}
TVDB_TOKEN = re.compile(r"\{tvdb-(\d+)\}", re.I)
# Spec §5.5 rule 8's test: two sources whose times are identical to the millisecond are copies, not witnesses.
COPY_TOLERANCE_S = 0.044


def edge(row: dict, source: str, kind: str, index: int) -> float | None:
    """The edge spec §5.5 rule 4 compares: intro end, credits start."""
    if source == "aniskip":
        value = row["aniskip"].get(kind)
    elif source in ("chapters", "plex"):
        value = row[source].get(kind)
    else:
        value = row[source]["by_type"].get(kind)
    return None if not value or value[index] is None else value[index]


def build(sample_size: int) -> list[dict]:
    """Ask every source about a random sample and return one row per episode."""
    sonarr = {s["tvdbId"]: s for s in json.loads((HERE / "sonarr_series.json").read_text()) if s.get("tvdbId")}
    sweep = [json.loads(line) for line in (HERE / "aniskip_sweep.jsonl").open() if '"error"' not in line]
    chapters = {c["file"]: c for c in json.loads((HERE / "anime_chapters.json").read_text()) if "err" not in c}
    plex = defaultdict(list)
    for marker in json.loads(PLEX_MARKERS.read_text()):
        plex[marker["file"]].append(marker)

    random.seed(41)
    picked = random.sample(sweep, min(sample_size, len(sweep)))
    clients = (
        ("theintrodb", theintrodb.TheIntroDbClient()),
        ("introdb", introdb.IntroDbClient()),
        ("skipdb", skipdb.SkipDbClient()),
    )

    def secs(ms: int | None) -> float | None:
        return None if ms is None else round(ms / 1000, 1)

    rows = []
    multi = Counter()  # segments dropped because the source already gave one of that type
    for entry in picked:
        tvdb = int(TVDB_TOKEN.search(entry["show"]).group(1))
        series = sonarr.get(tvdb, {})
        duration_ms = entry["duration_ms"]
        ids = MediaIds(
            "episode",
            str(series["tmdbId"]) if series.get("tmdbId") else None,
            series.get("imdbId"),
            str(tvdb),
            entry["season"],
            entry["episode"],
        )
        segments = {x["skipType"]: x for x in ((entry.get("body") or {}).get("results") or [])}
        aniskip = {}
        for kind, keys in PREFERENCE.items():
            for key in keys:
                if key in segments:
                    interval = segments[key]["interval"]
                    aniskip[kind] = [round(interval["startTime"], 1), round(interval["endTime"], 1)]
                    break
        row = {
            "show": entry["show"].split(" {")[0],
            "season": entry["season"],
            "episode": entry["episode"],
            "duration_s": round(duration_ms / 1000, 1) if duration_ms else None,
            "aniskip": aniskip,
        }
        for name, client in clients:
            result = client.lookup(ids, duration_ms=duration_ms, priority=0)
            # TheIntroDB returns an array per type (spec §4), so take the earliest, not whichever came first
            # in the response -- the same rule applied to Plex's markers below.
            by_type: dict[str, list] = {}
            for candidate in sorted(result.candidates, key=lambda c: c.start_ms):
                if candidate.type.value in by_type:
                    multi[f"{name} {candidate.type.value}"] += 1
                by_type.setdefault(candidate.type.value, [secs(candidate.start_ms), secs(candidate.end_ms)])
            row[name] = {"status": result.status, "by_type": by_type}
        probe = MediaProbe(
            duration_ms,
            tuple(
                Chapter(c["start_ms"], c["end_ms"], c["name"])
                for c in chapters.get(entry["file"], {}).get("chapters", [])
            ),
        )
        chapter_row: dict[str, list] = {}
        for candidate in chapter_candidates(probe):
            if candidate.type is MarkerType.INTRO:
                chapter_row.setdefault("intro", [secs(candidate.start_ms), secs(candidate.end_ms)])
            elif candidate.type is MarkerType.CREDITS:
                chapter_row["credits"] = [secs(candidate.start_ms), secs(candidate.end_ms)]
        row["chapters"] = chapter_row
        # Plex can hold several credits markers on one item; its *first* is the one the app compares (rule 7).
        plex_row: dict[str, list] = {}
        for marker in sorted(plex.get(entry["file"], []), key=lambda m: m["start"]):
            plex_row.setdefault(marker["type"], [secs(marker["start"]), secs(marker["end"])])
        row["plex"] = plex_row
        rows.append(row)
    (HERE / "compare.json").write_text(json.dumps(rows, indent=1))
    print("segments dropped as later duplicates of a type:", dict(multi) or "none")
    return rows


def report_coverage(rows: list[dict]) -> None:
    """Who answers what — the §4 coverage table."""
    counts = Counter()
    for row in rows:
        for kind in ("intro", "credits"):
            for source in SOURCES:
                counts[f"{source} {kind}"] += edge(row, source, kind, 0) is not None
            counts[f"any source {kind}"] += any(edge(row, s, kind, 0) is not None for s in SOURCES)
    print(f"\nsample of {len(rows)} anime episodes — who answers what:")
    for key in sorted(counts):
        print(f"   {key:22s} {counts[key]:4d}  ({counts[key] / len(rows):.0%})")


def report_independence(rows: list[dict]) -> None:
    """Spec §5.5 rule 8: how often two sources give times identical to the millisecond."""
    print(f"\n{'pair':34s} {'kind':8s} {'both':>5s} {'<=44 ms':>8s} {'<=1 s':>6s} {'<=5 s':>6s}")
    for first, second in itertools.combinations(SOURCES, 2):
        for kind, index in (("intro", 1), ("credits", 0)):
            deltas = []
            for row in rows:
                a, b = edge(row, first, kind, index), edge(row, second, kind, index)
                if a is not None and b is not None:
                    deltas.append(abs(a - b))
            if len(deltas) < 3:
                continue
            print(
                f"{first + ' vs ' + second:34s} {kind:8s} {len(deltas):5d}"
                f" {sum(d <= COPY_TOLERANCE_S for d in deltas):8d}"
                f" {sum(d <= 1 for d in deltas):6d} {sum(d <= 5 for d in deltas):6d}"
            )

    print("\nare the exact AniSkip/IntroDB matches just both copying the file's own chapter?")
    for kind, index in (("intro", 1), ("credits", 0)):
        exact = with_chapter = matching_chapter = 0
        for row in rows:
            a, b = edge(row, "aniskip", kind, index), edge(row, "introdb", kind, index)
            if a is None or b is None or abs(a - b) > COPY_TOLERANCE_S:
                continue
            exact += 1
            chapter = edge(row, "chapters", kind, index)
            if chapter is not None:
                with_chapter += 1
                matching_chapter += abs(a - chapter) <= 0.5
        print(
            f"  {kind}: {exact} exact pairs; {with_chapter} have a chapter of that type; "
            f"{matching_chapter} of those match it within 0.5 s"
        )


def report_marginal(rows: list[dict]) -> None:
    """What AniSkip adds that nothing else on the same file already has."""
    others = [s for s in SOURCES if s != "aniskip"]
    print("\nwhat AniSkip adds:")
    for kind in ("intro", "credits"):
        counts = Counter()
        for row in rows:
            mine = edge(row, "aniskip", kind, 0) is not None
            theirs = any(edge(row, s, kind, 0) is not None for s in others)
            online = any(edge(row, s, kind, 0) is not None for s in ("theintrodb", "introdb", "skipdb"))
            counts["aniskip answers"] += mine
            counts["only aniskip, of every source"] += mine and not theirs
            counts["only aniskip, of the online three"] += mine and not online
            counts["aniskip plus another (agreement possible)"] += mine and theirs
        print(f"  {kind}: " + ", ".join(f"{k} {v} ({v / len(rows):.0%})" for k, v in counts.items()))


def main() -> None:
    """Build the sample and print the §4 tables."""
    size = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    rows = build(size)
    report_coverage(rows)
    report_independence(rows)
    report_marginal(rows)


if __name__ == "__main__":
    main()
