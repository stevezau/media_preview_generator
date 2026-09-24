"""The 43 verified online cases through the real source parsers and decide() (ported from audit-phase1/online43_decide.py).

SkipDB is simulated from its ODbL dump by the nearest-duration row; ``match`` is exact (≤ 2 s), shifted (≤ 15 s) or
out-of-range by |dump duration − file duration|, as the read API labels it. The recorded answers are fed as the
setting has them on; ``extra`` adds more evidence per case (Plex's own markers, season audio) for the Plex comparison.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path

from media_preview_generator.markers.decide import DecisionContext, DecisionStatus, decide
from media_preview_generator.markers.models import Candidate, Marker, MarkerType
from media_preview_generator.markers.sources import introdb, skipdb, theintrodb

from .data import evidence_dir
from .decisions import g3_rule
from .plex import PlexMarker, first_marker

# The app's orders (settings.DEFAULT_GLOBAL_MARKERS with the pipeline's riders), TheIntroDB off and on.
DEFAULT_ORDER = ("chapters", "introdb", "skipdb", "season_audio", "season_audio_previous", "credits_text",
                 "server_markers", "server_markers_imported")  # fmt: skip
THEINTRODB_ORDER = ("chapters", "theintrodb", *DEFAULT_ORDER[1:])
SETTINGS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("Default sources, High", DEFAULT_ORDER, "high"),
    ("TheIntroDB on, High", THEINTRODB_ORDER, "high"),
    ("TheIntroDB on, Medium", THEINTRODB_ORDER, "medium"),
)
TYPES = (MarkerType.INTRO, MarkerType.CREDITS)
_SKIPDB_KEYS = ("intro", "outro", "recap", "preview")
_EPISODE_RE = re.compile(r"S(\d+)E(\d+)", re.I)


def load_online(evidence: Path | None = None) -> tuple[list[dict], list[dict]]:
    """``online/online_results.json`` (recorded TheIntroDB/IntroDB answers per case) and the SkipDB dump's segments."""
    root = evidence or evidence_dir()
    results = json.loads((root / "online/online_results.json").read_text())
    dump = json.loads((root / "online/skipdb-dump.json").read_text())["segments"]
    return results, dump


def case_key(case: dict) -> tuple[str, int, int]:
    """(imdb id, season, episode) of a case."""
    return case["imdb"], case["season"], case["episode"]


def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def case_file(case: dict, files: Iterable[str]) -> str | None:
    """The library file of a case: the one file whose show folder carries the case's show name and SxxEyy.

    Args:
        case: The case (show, season, episode).
        files: Library paths (``.../<show folder>/<season folder>/<file>``).

    Returns:
        The path, or None unless exactly one file matches.
    """
    show = _alnum(case["show"])
    found = []
    for path in files:
        parts = path.split("/")
        code = _EPISODE_RE.search(os.path.basename(path))
        if len(parts) < 3 or code is None or show not in _alnum(parts[-3]):
            continue
        if (int(code.group(1)), int(code.group(2))) == (case["season"], case["episode"]):
            found.append(path)
    return found[0] if len(found) == 1 else None


def skipdb_segments(case: dict, dump: list[dict]) -> dict[str, dict]:
    """The dump rows SkipDB's read API would return for this case, each with its ``match`` label."""
    rows = [
        x
        for x in dump
        if x["imdb_id"] == case["imdb"] and x.get("season") == case["season"] and x.get("episode") == case["episode"]
    ]
    out: dict[str, dict] = {}
    for key in _SKIPDB_KEYS:
        found = [x for x in rows if x["segment_type"] == key and x.get("start_ms") is not None]
        if not found:
            continue
        best = min(found, key=lambda x: abs((x.get("duration_ms") or 0) - case["dur"] * 1000))
        diff = abs((best.get("duration_ms") or 0) - case["dur"] * 1000)
        out[key] = {**best, "match": "exact" if diff <= 2000 else "shifted" if diff <= 15000 else "out-of-range"}
    return out


def judge_online(mtype: MarkerType, marker: Marker, case: dict) -> str:
    """The phase-1 audit's verdict: a wrong intro skips story (>5 s outside the truth); credits >10 s early are wrong."""
    start, end = marker.start_ms / 1000, marker.end_ms / 1000
    if mtype is MarkerType.INTRO:
        truth_start, truth_end = case["intro"]
        wrong = end > truth_end + 5 or start < truth_start - 5 or end < truth_start or start > truth_end
        return "wrong" if wrong else "useful"
    truth = case["credits_start"]
    if start < truth - 10:
        return "wrong"
    return "late" if start > truth + 30 else "useful"


def _truth(mtype: MarkerType, case: dict) -> object:
    return case["intro"] if mtype is MarkerType.INTRO else case["credits_start"]


def online_verdicts(
    results: list[dict],
    dump: list[dict],
    *,
    order: tuple[str, ...],
    level: str,
    extra: Mapping[tuple[str, int, int], list[Candidate]] | None = None,
    g3: bool = True,
    frame_rates: Mapping[tuple[str, int, int], float | None] | None = None,
) -> list[dict]:
    """Per case and type: the verdict (useful / late / wrong / missed) and what decided it.

    Args:
        results: ``online_results.json`` rows.
        dump: The SkipDB dump's segments.
        order: Source order; TheIntroDB, IntroDB and SkipDB answers count only when listed.
        level: ``high`` or ``medium``.
        extra: More candidates per ``case_key``.
        g3: Ruling G3 on (shipped) or off.
        frame_rates: Each case's file's probed video frame rate per ``case_key``, which the app's decisions read online
            times on the file's clock by (``decide`` rule 12); a case without one is decided without it.

    Returns:
        One entry per case and type with a truth: key, show, season, episode, type, verdict, the decided segment
        (seconds), decided_by and the decision's reason.
    """
    out = []
    for row in results:
        case = row["case"]
        candidates = []
        if "theintrodb" in order and isinstance(row.get("tidb"), dict):
            candidates += theintrodb._candidates(row["tidb"])
        if "introdb" in order and isinstance(row.get("idb"), dict):
            candidates += introdb._candidates(row["idb"])
        if "skipdb" in order:
            candidates += skipdb._candidates(skipdb_segments(case, dump))
        candidates += (extra or {}).get(case_key(case), [])
        rate = (frame_rates or {}).get(case_key(case))
        ctx = DecisionContext(int(case["dur"] * 1000), False, level, frozenset(TYPES), order, frame_rate=rate)
        with g3_rule(g3):
            decisions = decide(candidates, ctx, {})
        for mtype in TYPES:
            if _truth(mtype, case) is None:
                continue
            d = decisions[mtype]
            decided = d.status is DecisionStatus.DECIDED
            out.append({"key": case_key(case), "show": case["show"], "season": case["season"],
                        "episode": case["episode"], "type": mtype.value,
                        "verdict": judge_online(mtype, d.marker, case) if decided else "missed",
                        "segment": (d.marker.start_ms / 1000, d.marker.end_ms / 1000) if decided else None,
                        "decided_by": d.marker.decided_by if decided else (), "reason": d.reason})  # fmt: skip
    return out


def tally(verdicts: list[dict]) -> dict[str, Counter]:
    """Verdict counts per type (``intro``, ``credits``)."""
    counts = {mtype.value: Counter() for mtype in TYPES}
    for v in verdicts:
        counts[v["type"]][v["verdict"]] += 1
    return counts


def run_online(
    results: list[dict],
    dump: list[dict],
    *,
    order: tuple[str, ...],
    level: str,
    extra: Mapping[tuple[str, int, int], list[Candidate]] | None = None,
    g3: bool = True,
    frame_rates: Mapping[tuple[str, int, int], float | None] | None = None,
) -> dict[str, Counter]:
    """Counts per type (``intro``, ``credits``): useful / late / wrong / missed (not decided). See ``online_verdicts``."""
    return tally(online_verdicts(results, dump, order=order, level=level, extra=extra, g3=g3, frame_rates=frame_rates))


def plex_online(results: list[dict], baseline: Mapping[tuple[str, int, int], list[PlexMarker]]) -> dict[str, Counter]:
    """Plex's own markers judged like our decisions: its first intro and its first credits start per case.

    Args:
        results: ``online_results.json`` rows.
        baseline: Plex's markers per ``case_key`` (a case without an entry counts as missed).

    Returns:
        Counts per type.
    """
    counts = {mtype.value: Counter() for mtype in TYPES}
    for row in results:
        case = row["case"]
        for mtype in TYPES:
            if _truth(mtype, case) is None:
                continue
            m = first_marker(list(baseline.get(case_key(case), [])), mtype)
            marker = Marker(mtype, m.start_ms, m.end_ms, ("plex",)) if m else None
            counts[mtype.value][judge_online(mtype, marker, case) if marker else "missed"] += 1
    return counts
