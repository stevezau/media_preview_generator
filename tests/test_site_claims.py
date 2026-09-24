"""Claims on the site, the READMEs and llms.txt must be checkable.

Spec rule (docs/design/site-redesign.md §1, §8): a speed number is published only from
docs/benchmark/summary.json, which a test recomputes from the raw results. A "5x faster" typed into a
card or a README has no source and goes stale the day the benchmark is re-run. The stats under the
landing page's hero are quoted by people and by AI agents, so the ones that describe the app are
checked against the code here too.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from flask import Flask, request

from media_preview_generator.web.webhook_router import _classify_payload
from scripts.generate_llms_full import excluded_folders

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"
STATS = DOCS / "_data" / "stats.yml"
SUMMARY = DOCS / "benchmark" / "summary.json"
BENCHMARK_PAGE = DOCS / "benchmark.md"
# Any multiplier ("15–60×", "~17×", "3x") or frame rate, not only a number followed by "faster": the
# figures a review had to remove by hand were bare multipliers, and the old pattern missed them all.
CLAIM = re.compile(
    r"(?i)\b\d+(?:\.\d+)?\s*(?:x|×|times)\s+(?:faster|quicker|slower|as fast)\b"
    r"|\b\d+(?:\.\d+)?\s*%\s+(?:faster|quicker|slower|less time)\b"
    r"|(?<![\w×])~?\d+(?:\.\d+)?(?:\s*[–-]\s*\d+(?:\.\d+)?)?\s*[×x](?![\w×])"
    r"|\b\d+\s*fps\b"
)
# Multipliers that aren't about speed, and log lines the docs quote word for word (the credit-text
# self-test's reason, markers/credits/textdet_helper.py), as they appear in the sources.
NOT_SPEED = ("2x capture", "4× safety buffer", "10% faster than the CPU (median")
# docs/faq.md at 5f0960c7: unbenchmarked figures that shipped while the old pattern passed.
OLD_FAQ_SPEEDS = """
- **SDR / HDR10 / HDR10+ / HLG / Dolby Vision Profile 7/8** — 15–60× across all GPU vendors.
  - **Intel** (iGPU, Arc): ~17× — fastest path. Just needs the GPU passed to the container.
  - **NVIDIA**: ~10–16× — needs `NVIDIA_DRIVER_CAPABILITIES=all`.
  - **AMD / Apple / CPU-only**: ~5–10× via software decode.
"""

# What each sender posts when set up as docs/guides.md and docs/multi-server.md describe, trimmed to
# the fields the router looks at. Plex posts multipart form data; the rest post JSON.
SENDER_BODIES: dict[str, dict] = {
    "Sonarr": {"eventType": "Download", "series": {"path": "/tv/Show"}, "episodeFile": {"path": "/tv/Show/a.mkv"}},
    "Radarr": {"eventType": "Download", "movie": {"folderPath": "/films/F"}, "movieFile": {"path": "/films/F/f.mkv"}},
    "Plex": {"event": "library.new", "Server": {"uuid": "plex-id"}, "Metadata": {"ratingKey": "1"}},
    "Emby": {"Event": "library.new", "Server": {"Id": "emby-id"}, "Item": {"Id": "1"}},
    "Jellyfin": {"NotificationType": "ItemAdded", "ServerId": "jellyfin-id", "ItemId": "1"},
    "Tdarr": {"file_path": "/films/F/f.mkv"},
    "FileFlows": {"file_path": "/films/F/f.mkv"},
}
# Named on the site but with no body above: never counted as recognised.
OTHER_SENDERS = ["Sportarr"]


def _sources() -> list[Path]:
    pages = [
        p
        for p in DOCS.rglob("*.md")
        if not excluded_folders(DOCS) & set(p.relative_to(DOCS).parts) and p != BENCHMARK_PAGE
    ]
    return sorted(
        [
            REPO_ROOT / "README.md",
            REPO_ROOT / "DOCKERHUB_README.md",
            DOCS / "llms.txt",
            *pages,
            *DOCS.glob("_data/*.yml"),
            *DOCS.glob("_layouts/*.html"),
            *DOCS.glob("_includes/*.html"),
        ]
    )


def _stats() -> list[dict]:
    return yaml.safe_load(STATS.read_text(encoding="utf-8"))


def _kind_at_the_universal_url(sender: str) -> str:
    """How POST /api/webhooks/incoming classifies this sender's body ("unknown" if it can't)."""
    body = SENDER_BODIES[sender]
    if sender == "Plex":
        context = {"method": "POST", "data": {"payload": json.dumps(body)}, "content_type": "multipart/form-data"}
    else:
        context = {"method": "POST", "json": body}
    with Flask(__name__).test_request_context("/api/webhooks/incoming", **context):
        return _classify_payload(request)[0]


def _speed_claims(text: str) -> list[str]:
    return [m.group(0) for m in CLAIM.finditer(text) if not any(text.startswith(ok, m.start()) for ok in NOT_SPEED)]


@pytest.mark.parametrize("source", _sources(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_no_speed_claim_outside_the_benchmark(source: Path) -> None:
    claims = _speed_claims(source.read_text(encoding="utf-8"))
    assert not claims, (
        f"{source.relative_to(REPO_ROOT)} states a speed number: {claims}. Use a stat with source: benchmark."
    )


def test_speed_pattern_catches_bare_multipliers() -> None:
    assert _speed_claims(OLD_FAQ_SPEEDS) == ["15–60×", "~17×", "~10–16×", "~5–10×"]
    assert _speed_claims("Decodes at 240 fps. A 3x speedup.") == ["240 fps", "3x"]
    assert _speed_claims("A 1920x1080 frame, 0x1f, x86_64, half the 2x capture's pixels.") == []


def test_every_stat_names_its_source() -> None:
    stats = _stats()
    assert stats
    assert all(stat.get("source") in {"fact", "benchmark"} for stat in stats)


def test_benchmark_stats_equal_the_summary() -> None:
    benchmark_stats = [s for s in _stats() if s["source"] == "benchmark"]
    if not benchmark_stats:
        pytest.skip("stats.yml states no speed number: the owner chose none at STOP 3 (2026-09-24)")
    # The landing page links a benchmark stat to /benchmark/, which only this page builds.
    assert BENCHMARK_PAGE.is_file(), "a benchmark stat links /benchmark/, so it needs docs/benchmark.md"
    assert SUMMARY.is_file(), "a benchmark stat needs docs/benchmark/summary.json"
    summary = json.loads(SUMMARY.read_text(encoding="utf-8"))
    assert summary["ratio_display"] is not None, f"no ratio to publish: {summary['reason']}"
    assert [s["value"] for s in benchmark_stats] == [summary["ratio_display"]] * len(benchmark_stats)


def test_the_universal_url_recognises_each_sender_it_should() -> None:
    # Pins the table below to the router, so a sender dropping out of /incoming fails here first.
    kinds = {sender: _kind_at_the_universal_url(sender) for sender in SENDER_BODIES}
    assert kinds == {
        "Sonarr": "sonarr",
        "Radarr": "radarr",
        "Plex": "plex",
        "Emby": "emby",
        "Jellyfin": "jellyfin",
        "Tdarr": "unknown",
        "FileFlows": "unknown",
    }


def test_the_one_webhook_url_stat_names_only_senders_that_url_recognises() -> None:
    # "1 webhook URL" is true for the senders POST /api/webhooks/incoming tells apart by payload, and
    # for nobody else: Tdarr and FileFlows post {"file_path": ...}, which only /custom accepts.
    (stat,) = [s for s in _stats() if s["label"] == "webhook URL"]
    assert stat["value"] == "1"
    named = [sender for sender in [*SENDER_BODIES, *OTHER_SENDERS] if sender in stat["note"]]
    assert named, "the stat should say which senders share the URL"
    recognised = {sender for sender in SENDER_BODIES if _kind_at_the_universal_url(sender) != "unknown"}
    assert [sender for sender in named if sender not in recognised] == []
