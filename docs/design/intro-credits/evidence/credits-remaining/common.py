"""Shared helpers for the 2026-09-27 remaining-credits lane (evidence, not shipped).

Answers come from two local-only files per tree (gitignored, they name library paths): the verdict/Plex/chapter files'
credit text (``../credits-accuracy/vtext.py``) and the harness sets' (``allsets.py``). Truth is the audit's frame check
(``../credits-accuracy/items.json`` with ``checks.json``) or the harness set's own.
"""

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ACCURACY = HERE.parent / "credits-accuracy"
REPO = HERE.parents[4]
sys.path.insert(0, os.environ.get("CREDFIX_WORK", str(REPO)))

from media_preview_generator.markers.credits import rule_j  # noqa: E402
from tools.markers_eval.decode_cache import rows_from_json  # noqa: E402

TOL = 5.0


def audit_truths() -> dict[str, dict]:
    """Frame-checked credits truth per path from the audit's verdict and Plex sets (a check overrides the item)."""
    items = json.load(open(ACCURACY / "items.json"))
    checks = json.load(open(ACCURACY / "checks.json")) if (ACCURACY / "checks.json").exists() else {}
    out = {}
    for it in items["verdict"] + items["plex"]:
        if it["type"] != "credits":
            continue
        truth = checks.get(it["id"], it.get("truth"))
        if truth is None:
            continue
        # The verdict set's frame check wins over the Plex comparison's "our marker was right" on the same file.
        if it["path"] in out and not out[it["path"]].get("approx") and truth.get("approx"):
            continue
        out[it["path"]] = {**truth, "id": it["id"]}
    return out


def verdict(start: float | None, truth: dict | float) -> str:
    """right / early / late / none against a truth (a range with lo/hi, or one start), 5 s either way."""
    if start is None:
        return "none"
    if isinstance(truth, dict):
        lo, hi = truth.get("lo", truth["start"]), truth.get("hi", truth["start"])
    else:
        lo = hi = truth
    if start < lo - TOL:
        return "early"
    if start > hi + TOL:
        return "late"
    return "right"


def rule_rows(result: dict) -> tuple[list, list, list]:
    """The keyframe rows as decoded, the rows the rule reads (overlays dropped) and the 1 fps rows (overlays dropped)."""
    overlays = [tuple(b) for b in result["overlays"]]
    key = rows_from_json(result["key"])
    return (
        key,
        rule_j.without_overlays(key, overlays),
        rule_j.without_overlays(rows_from_json(result["fine"]), overlays),
    )
