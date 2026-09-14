"""Loaders for the local-only truth sets under docs/design/intro-credits/evidence (git-ignored: real library paths)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

EVIDENCE_ENV = "MARKERS_EVAL_EVIDENCE"


def evidence_dir() -> Path:
    """``$MARKERS_EVAL_EVIDENCE``, else this checkout's evidence folder."""
    env = os.environ.get(EVIDENCE_ENV)
    return Path(env) if env else Path(__file__).resolve().parents[2] / "docs/design/intro-credits/evidence"


@dataclass(frozen=True)
class EvalEpisode:
    """One episode of the 118-episode intro eval."""

    season: str
    file: str
    truth_intro: tuple[float, float] | None
    truth_credits: tuple[float, float] | None
    v3_segment: tuple[float, float, int] | None


def _pair(value: object) -> tuple[float, float] | None:
    return (float(value[0]), float(value[1])) if isinstance(value, list) and len(value) >= 2 else None


def load_v3_results(path: Path | None = None) -> list[EvalEpisode]:
    """``eval/eval_results_v3.json``: season, file, chapter truth and the v3 intro segment per episode."""
    rows = json.loads((path or evidence_dir() / "eval/eval_results_v3.json").read_text())
    out = []
    for r in rows:
        seg = (r.get("intro") or {}).get("segment")
        out.append(
            EvalEpisode(
                season=r["season"],
                file=r["file"],
                truth_intro=_pair((r.get("truth") or {}).get("intro")),
                truth_credits=_pair((r.get("truth") or {}).get("credits")),
                v3_segment=(float(seg[0]), float(seg[1]), int(seg[2])) if seg else None,
            )
        )
    return out


def by_season(episodes: list[EvalEpisode]) -> dict[str, list[EvalEpisode]]:
    """Episodes grouped by season folder, both sorted (the eval matched each season's files in path order)."""
    groups: dict[str, list[EvalEpisode]] = {}
    for e in episodes:
        groups.setdefault(e.season, []).append(e)
    return {s: sorted(groups[s], key=lambda e: e.file) for s in sorted(groups)}
