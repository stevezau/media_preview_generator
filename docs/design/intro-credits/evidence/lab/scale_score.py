#!/usr/bin/env python3
"""Phase 1 scale run (plan Task 20 Step 4): picking the lab mounts, truth, and scoring.

    ./scale_score.py pick      choose the seasons/movies to mount -> results/scale/pick.json + scale_mounts.sh (git-ignored)
    ./scale_score.py truth     chapter / movie-credits / online-cases truth per mounted file -> results/scale/truth.json
    ./scale_score.py score     score results/scale/collected.json (from `phase1_matrix.py scale collect`) against the
                               truth and the prod Plex markers -> results/scale/score.json

Everything under results/ is git-ignored (it lists library paths). Prod Plex markers come from a read-only dump
(`phase1_matrix.py scale prod-dump`). The repo is public: only aggregate numbers and show/movie names leave results/.
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

LAB = Path(__file__).resolve().parent
EVIDENCE = LAB.parent
OUT = LAB / "results" / "scale"
VIDEO = (".mkv", ".mp4", ".m4v", ".avi", ".webm")
TV_TARGET_NAMED_EPISODES = 420
MOVIE_TARGET = 100
SEED = 20

# Named-chapter seasons Plex has no markers for, kept for their chapter shapes (pick reason in brackets).
NAMED_EXTRA = {
    ("Mushoku Tensei Jobless Reincarnation (2021) {tvdb-371310}", "Season 01"): "anime: Intro + End Credits + Preview",
    ("Crime in Progress (2026) {tvdb-471523}", "Season 01"): "a 'Credits' chapter first (opening credits)",
    ("Common Side Effects (2025) {tvdb-436198}", "Season 01"): "cold open: Scene, Recap, Scene, Intro",
    ("The Gone Game (2020) {tvdb-387149}", "Season 02"): "'Intro Start' / 'Credit Start' names",
    ("Help! I Wrecked My House (2020) {tvdb-387269}", "Season 02"): "'Credits' between two chapters",
    ("St. Denis Medical (2024) {tvdb-435916}", "Season 01"): "'Outro' credits name",
}
# Named seasons with prod markers that must be in (online/cases.json shows, chapter shapes that broke things before).
NAMED_FORCED = {
    ("The Simpsons (1989) {tvdb-71663}", "Season 03"): "online/cases.json; long show",
    ("The Simpsons (1989) {tvdb-71663}", "Season 09"): "long show",
    ("How I Met Your Mother (2005) {tvdb-75760}", "Season 07"): "online/cases.json; long show",
    ("How I Met Your Mother (2005) {tvdb-75760}", "Season 08"): "long show",
    ("Outlander (2014) {tvdb-270408}", "Season 08"): "online/cases.json; 4K HDR",
    ("Marvels Daredevil (2015) {tvdb-281662}", "Season 03"): "online/cases.json; 4K HDR; Intro + End of Intro",
    ("JUJUTSU KAISEN (2020) {tvdb-377543}", "Season 02"): "anime: Intro (cold open) + Opening + Ending",
    ("Food Wars! (2015) {tvdb-289909}", "Season 01"): "anime: Prologue, Opening, Eyecatch, Ending",
    ("Re ZERO Starting Life in Another World (2016) {tvdb-305089}", "Season 02"): "anime: Avant + ED",
    ("My Hero Academia (2016) {tvdb-305074}", "Season 07"): "anime: Intro, Episode, End Credits, Preview",
    ("Record of Ragnarok (2021) {tvdb-393810}", "Season 02"): "anime: Opening Credits / End Credits",
    ("Ninja Kamui (2024) {tvdb-420280}", "Season 01"): "anime; cold open before Intro",
    ("Alias (2001) {tvdb-75930}", "Season 02"): "Recap, cold open, Intro",
    ("The Kardashians (2022) {tvdb-414093}", "Season 06"): "Credits followed by a Post-Credits Scene",
    ("Avenue 5 (2020) {tvdb-364106}", "Season 02"): "'Credits' between two chapters; 4K HDR",
    ("Law and Order Criminal Intent (2001) {tvdb-71489}", "Season 09"): "Intro + Main Content",
    ("Jurassic World Camp Cretaceous (2020) {tvdb-365066}", "Season 05"): "Intro + Intro End",
    ("Scooby-Doo and Guess Who (2019) {tvdb-361065}", "Season 02"): "'Intro start' / 'Intro end' names",
    ("Six Dreams (2018) {tvdb-350727}", "Season 01"): "Opening Credits after a cold open; sports doc",
    ("Mr. Robot (2015) {tvdb-289590}", "Season 04"): "Previously On + Act chapters",
    ("Community (2009) {tvdb-94571}", "Season 04"): "4K HDR; Opening Credits",
    ("Succession (2018) {tvdb-338186}", "Season 04"): "4K HDR; cold open",
}
# Seasons outside named_seasons.json: (show folder, season folder, wanted file count of the copy) -> reason.
VARIETY_TV = {
    ("SPY x FAMILY (2022) {tvdb-405920}", "Season 01", 20): "anime: OP / ED / PV chapters",
    ("Chainsaw Man (2022) {tvdb-397934}", "Season 01", 5): "anime: Prologue, Opening, Ending; 4K",
    ("Severance (2022) {tvdb-371980}", "Season 01", 4): "French chapter names; 4K HDR",
    ("Frieren Beyond Journeys End (2023) {tvdb-424536}", "Season 01", 7): "anime without chapters; 4K HDR",
    ("Cowboy Bebop (2021) {tvdb-367234}", "Season 01", 7): "no chapters; 4K HDR",
    ("How I Met Your Mother (2005) {tvdb-75760}", "Season 04", 18): "mp4, no chapters; third season of a long show",
    ("The Office US (2005) {tvdb-73244}", "Season 02", 20): "mp4, no chapters",
    ("Stranger Things (2016) {tvdb-305288}", "Season 01", 5): "no chapters; HDR",
    ("Doctor Who (2005) {tvdb-78804}", "Specials", 10): "Specials (season 0); mkv + mp4",
    ("Bluey (2018) {tvdb-353546}", "Season 01", 17): "7-minute episodes; Intro/Scene/Credits chapters",
    ("Dateline NBC (1992) {tvdb-70600}", "Season 33", 10): "date-named episodes (no SxxEyy)",
    ("Planet Earth II (2016) {tvdb-318408}", "Season 01", 4): "timestamp-named chapters; documentary",
}
# Same host folders mounted under the id-less show folders up.sh already uses (no tvdb id in the path).
VARIETY_TV_NO_ID = {
    ("Rick and Morty (2013) {tvdb-275274}", "Season 02", 7, "Rick and Morty (2013)"): "no tvdb id in path; "
    "timestamp-named chapters",
    ("South Park (1997) {tvdb-75897}", "Specials", 1, "South Park (1997)"): "no tvdb id in path; Specials",
}
ROOTS = ("/data_16tb", "/data_16tb2", "/data_16tb3")


def load(name: str) -> object:
    return json.loads((EVIDENCE / name).read_text())


def videos(folder: str) -> list[str]:
    try:
        return sorted(e for e in os.listdir(folder) if e.lower().endswith(VIDEO))
    except OSError:
        return []


def prod_marker_files() -> dict[str, set[str]]:
    """Folder -> files the prod Plex has its own intro/credits markers for."""
    out: dict[str, set[str]] = defaultdict(set)
    for row in json.loads((OUT / "prod_plex_markers.json").read_text()):
        out[os.path.dirname(row["file"])].add(row["file"])
    return out


def safe_for_docker(path: str) -> bool:
    return not any(ch in path for ch in ':,"$`\\')


def pick() -> dict:
    prod = prod_marker_files()
    rng = random.Random(SEED)
    best: dict[tuple[str, str], tuple] = {}
    for season, _n, names in load("eval/named_seasons.json"):
        show, folder = season.split("TV Shows/")[1].split("/")
        cand = (len(prod[season]), len(videos(season)), season, names[:8])
        if (show, folder) not in best or cand[:2] > best[(show, folder)][:2]:
            best[(show, folder)] = cand

    tv: list[dict] = []

    def add_tv(host: str, container: str, reason: str) -> None:
        if not safe_for_docker(host) or any(t["host"] == host or t["container"] == container for t in tv):
            return
        files = videos(host)
        tv.append(
            {
                "host": host,
                "container": container,
                "reason": reason,
                "episodes": len(files),
                "prod_marker_files": len(prod[host]),
            }
        )

    for key, reason in NAMED_FORCED.items():
        _, _, host, _ = best[key]
        add_tv(host, f"/media/tv/{key[0]}/{key[1]}", f"named chapters; {reason}")
    pool = sorted(k for k, v in best.items() if v[0] > 0 and k not in NAMED_FORCED)
    rng.shuffle(pool)
    for key in pool:
        if sum(t["episodes"] for t in tv) >= TV_TARGET_NAMED_EPISODES:
            break
        add_tv(best[key][2], f"/media/tv/{key[0]}/{key[1]}", f"named chapters {best[key][3][:4]}")
    for key, reason in NAMED_EXTRA.items():
        add_tv(best[key][2], f"/media/tv/{key[0]}/{key[1]}", f"named chapters, no Plex markers; {reason}")

    def find_copy(show: str, folder: str, count: int) -> str:
        for root in ROOTS:
            host = f"{root}/TV Shows/{show}/{folder}"
            if len(videos(host)) == count:
                return host
        raise SystemExit(f"no copy of {show}/{folder} with {count} files")

    for (show, folder, count), reason in VARIETY_TV.items():
        add_tv(find_copy(show, folder, count), f"/media/tv/{show}/{folder}", reason)
    for (show, folder, count, lab_show), reason in VARIETY_TV_NO_ID.items():
        add_tv(find_copy(show, folder, count), f"/media/tv/{lab_show}/{folder}", reason)

    movies: list[dict] = []
    truth = load("credits/movie_credit_truth.json")
    adjudicated = set(load("credits/adjudicated.json"))

    def add_movie(entry: dict, reason: str) -> None:
        host = os.path.dirname(entry["file"])
        name = os.path.basename(host)
        if not safe_for_docker(host) or any(m["host"] == host for m in movies):
            return
        if name.startswith(("Toy Story (1995)", "Up (2009)")):
            return
        entries = os.listdir(host)
        movies.append(
            {
                "host": host,
                "container": f"/media/movies/{name}",
                "reason": reason,
                "videos": len([e for e in entries if e.lower().endswith(VIDEO)]),
                "subfolders": sorted(e for e in entries if os.path.isdir(os.path.join(host, e))),
                "prod_credits": entry["file"] in prod[host],
            }
        )

    for entry in truth:
        base = os.path.basename(entry["file"])
        folder = os.path.dirname(entry["file"])
        if base in adjudicated:
            add_movie(entry, "credits truth adjudicated by frame checks")
        elif entry["file"].endswith(".mp4"):
            add_movie(entry, "mp4; 4K HDR")
        elif any(os.path.isdir(os.path.join(folder, e)) for e in os.listdir(folder)):
            add_movie(entry, "extras subfolder")
    rest = [e for e in truth if e["file"] in prod[os.path.dirname(e["file"])]]
    rng.shuffle(rest)
    rest.sort(key=lambda e: not re.search(r"2160p", e["file"]))  # stable: 4K first, shuffled within
    for entry in rest:
        if len(movies) >= MOVIE_TARGET:
            break
        hdr = "4K HDR/DV" if re.search(r"HDR|\bDV\b", entry["file"]) else "4K" if "2160p" in entry["file"] else ""
        add_movie(entry, f"credits truth{'; ' + hdr if hdr else ''}")

    result = {
        "tv": tv,
        "movies": movies,
        "totals": {
            "tv_folders": len(tv),
            "tv_episodes": sum(t["episodes"] for t in tv),
            "tv_with_prod_markers": sum(t["prod_marker_files"] for t in tv),
            "movie_folders": len(movies),
            "movie_videos": sum(m["videos"] for m in movies),
            "movies_with_prod_credits": sum(m["prod_credits"] for m in movies),
            "movies_with_subfolders": sum(bool(m["subfolders"]) for m in movies),
        },
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "pick.json").write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps(result["totals"]))
    return result


def mv_block() -> str:
    data = json.loads((OUT / "pick.json").read_text())
    lines = ["MV_SCALE=("]
    for entry in data["tv"] + data["movies"]:
        lines.append(f'    -v "{entry["host"]}:{entry["container"]}:ro"')
    lines.append(")")
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------------------ truth

INTRO_TRUTH = re.compile(r"(?i)^(intro|opening|opening credits|opening titles|title sequence|main titles?|op)$")
CREDITS_TRUTH = re.compile(r"(?i)^(credits|end credits|closing credits|outro|ending|ed)$")


def ffprobe_chapters(path: str) -> dict:
    out = subprocess.run(
        [
            "nice",
            "-n",
            "19",
            "ffprobe",
            "-v",
            "error",
            "-show_chapters",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout
    data = json.loads(out or "{}")
    chapters = [
        (c.get("tags", {}).get("title", "").lstrip("﻿").strip(), float(c["start_time"]), float(c["end_time"]))
        for c in data.get("chapters", [])
    ]
    return {"duration": float(data.get("format", {}).get("duration") or 0), "chapters": chapters}


def chapter_truth(chapters: list[tuple[str, float, float]]) -> dict:
    """Eval truth from studio chapter names (the eval/run_eval_v3.py rules): first intro-like, last credits-like.

    A generic "Intro" next to a specific opening name ("OP", "Opening", ...) is the cold open, so the specific one is
    the intro; ends run to the next chapter's start.
    """
    names = [n for n, _, _ in chapters]
    specific = [i for i, n in enumerate(names) if INTRO_TRUTH.match(n) and n.lower() != "intro"]
    truth = {}
    for i, (name, start, end) in enumerate(chapters):
        nxt = chapters[i + 1][1] if i + 1 < len(chapters) else end
        if INTRO_TRUTH.match(name) and "intro" not in truth and (not specific or i in specific):
            truth["intro"] = (round(start * 1000), round(min(end, nxt) * 1000), name)
        if CREDITS_TRUTH.match(name):
            truth["credits"] = (round(start * 1000), round(min(end, nxt) * 1000), name)
    return truth


def build_truth() -> dict:
    data = json.loads((OUT / "pick.json").read_text())
    files: list[tuple[str, str]] = []
    for entry in data["tv"] + data["movies"]:
        for name in videos(entry["host"]):
            files.append((f"{entry['host']}/{name}", f"{entry['container']}/{name}"))
    for extra in (
        (
            "/data_16tb2/TV Shows/Rick and Morty (2013) {tvdb-275274}/Season 01",
            "/media/tv/Rick and Morty (2013)/Season 01",
        ),
        ("/data_16tb/TV Shows/South Park (1997) {tvdb-75897}/Season 01", "/media/tv/South Park (1997)/Season 01"),
        ("/data_16tb/Movies/Toy Story (1995) {tmdb-862}", "/media/movies/Toy Story (1995)"),
        ("/data_16tb/Movies/Up (2009) {tmdb-14160}", "/media/movies/Up (2009)"),
    ):
        for name in videos(extra[0]):
            files.append((f"{extra[0]}/{name}", f"{extra[1]}/{name}"))
    with ThreadPoolExecutor(4) as pool:
        probes = list(pool.map(lambda f: ffprobe_chapters(f[0]), files))
    movie_truth = {e["file"]: e for e in load("credits/movie_credit_truth.json")}
    adjudicated = load("credits/adjudicated.json")
    cases = load("online/cases.json")
    out = {}
    for (host, container), probe in zip(files, probes, strict=True):
        entry = {
            "host": host,
            "duration_ms": round(probe["duration"] * 1000),
            "chapters": [[c[0], round(c[1] * 1000), round(c[2] * 1000)] for c in probe["chapters"]],
        }
        truth = chapter_truth(probe["chapters"])
        entry["truth"] = {k: {"start": v[0], "end": v[1], "src": f"chapter '{v[2]}'"} for k, v in truth.items()}
        if host in movie_truth:
            start = round(movie_truth[host]["credits_start"] * 1000)
            src = "movie_credit_truth.json"
            if os.path.basename(host) in adjudicated:
                start, src = round(adjudicated[os.path.basename(host)]["truth"] * 1000), "adjudicated.json"
            entry["truth"]["credits"] = {"start": start, "end": entry["duration_ms"], "src": src}
        match = re.search(r"S(\d+)E(\d+)", os.path.basename(host))
        for case in cases:
            if (
                match
                and case["show"] in host
                and case["season"] == int(match.group(1))
                and case["episode"] == int(match.group(2))
            ):
                if abs(case["dur"] * 1000 - entry["duration_ms"]) < 2000:
                    entry["truth"].setdefault(
                        "intro",
                        {
                            "start": round(case["intro"][0] * 1000),
                            "end": round(case["intro"][1] * 1000),
                            "src": f"cases.json {case['truth_src']}",
                        },
                    )
                    entry["truth"].setdefault(
                        "credits",
                        {
                            "start": round(case["credits_start"] * 1000),
                            "end": entry["duration_ms"],
                            "src": f"cases.json {case['truth_src']}",
                        },
                    )
        out[container] = entry
    (OUT / "truth.json").write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(
        f"{len(out)} files; with intro truth {sum('intro' in e['truth'] for e in out.values())}; "
        f"with credits truth {sum('credits' in e['truth'] for e in out.values())}; "
        f"no chapters {sum(not e['chapters'] for e in out.values())}"
    )
    return out


# ------------------------------------------------------------------------------------------------------------ score

TOL_MS = 3_000
PREVIEW_NAME = re.compile(r"(?i)^(preview|pv|next (episode|time)( preview)?|next on\b.*|coming up next)$")
REPO = LAB.parents[4]
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


def content_after_credits(entry: dict, truth_end: int) -> bool:
    """Whether story content follows the credits chapter (a post-credits scene), not just a preview or nothing."""
    after = [c for c in entry.get("chapters") or [] if c[1] >= truth_end - 500 and c[2] - c[1] > TOL_MS]
    return any(not PREVIEW_NAME.match(c[0]) for c in after) and entry["duration_ms"] - truth_end > TOL_MS


def grade(mtype: str, marker: tuple[int, int] | None, truth: dict, after: bool, tol: int = TOL_MS) -> str:
    """right / none / wrong:<why>, judged at the boundary that matters (intro end, credits start) plus skipped story."""
    if marker is None:
        return "none"
    start, end = marker
    if mtype == "intro":
        if end > truth["end"] + tol:
            return "wrong:intro ends late (skips story)"
        if start < truth["start"] - tol:
            return "wrong:intro starts early (skips story)"
        if end < truth["end"] - tol:
            return "wrong:intro ends early"
        return "right"
    if start < truth["start"] - tol:
        return "wrong:credits start early (skips story)"
    if start > truth["start"] + tol:
        return "wrong:credits start late"
    if after and end > truth["end"] + tol:
        return "wrong:credits run into a post-credits scene"
    return "right"


def plex_marker(rows: list[dict], mtype: str, truth: dict) -> tuple[tuple[int, int] | None, int]:
    """Prod Plex's served marker of a type nearest the truth start (credits: +2 s start, non-final -2 s end)."""
    served = []
    for row in rows:
        if row["type"] != mtype:
            continue
        start, end = row["start"], row["end"] if row["end"] is not None else row["duration"]
        if mtype == "credits":
            final = '"pv:final":"1"' in (row.get("extra") or "")
            start, end = start + 2_000, end if final else end - 2_000
        served.append((start, end))
    if not served:
        return None, 0
    return min(served, key=lambda m: abs(m[0] - truth["start"])), len(served)


def redecide(entry: dict, *, publish_when: str, drop: tuple[str, ...] = ()) -> dict[str, tuple[int, int, tuple] | None]:
    """The app's own decide() over the evidence the backfill stored, optionally without some sources."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from media_preview_generator.markers.decide import DecisionContext, DecisionStatus, decide  # noqa: PLC0415
    from media_preview_generator.markers.models import Candidate, MarkerType, Source  # noqa: PLC0415

    candidates = [
        Candidate(
            MarkerType(e["type"]),
            e["start_ms"],
            e["end_ms"],
            Source(e["source"]),
            1.0 if e["confidence"] is None else e["confidence"],
            e.get("origin") or "",
        )
        for e in entry.get("evidence") or []
        if e.get("type") and e["source"] not in drop
    ]
    types = frozenset({MarkerType.CREDITS} if entry["is_movie"] else {MarkerType.CREDITS, MarkerType.INTRO})
    order = tuple(s for s in SOURCE_ORDER if s not in drop)
    ctx = DecisionContext(entry["duration_ms"] or 0, bool(entry["is_movie"]), publish_when, types, order)
    out = {}
    for mtype, decision in decide(candidates, ctx, {}).items():
        if mtype.value in ("intro", "credits"):
            m = decision.marker if decision.status is DecisionStatus.DECIDED else None
            out[mtype.value] = (m.start_ms, m.end_ms, m.decided_by) if m else None
    return out


def show_of(container: str) -> str:
    parts = container.split("/")
    name = parts[3]
    return re.sub(r"\s*\{(tvdb|tmdb|imdb)-[^}]*\}", "", name)


def score(name: str = "backfill") -> dict:
    truth = json.loads((OUT / "truth.json").read_text())
    collected = json.loads((OUT / f"collected-{name}.json").read_text())
    prod_rows: dict[str, list[dict]] = defaultdict(list)
    for row in json.loads((OUT / "prod_plex_markers.json").read_text()):
        prod_rows[row["file"]].append(row)
    prod_files = {p["file"] for p in json.loads((OUT / "prod_plex_parts.json").read_text())}

    tallies: dict[str, Counter] = defaultdict(Counter)
    shows: dict[str, Counter] = defaultdict(Counter)
    detail = []
    for entry in collected:
        t_entry = truth.get(entry["file"])
        if not t_entry or not entry.get("decisions"):
            tallies["files"]["not scored (no truth entry: synth)"] += 1
            continue
        tallies["files"]["scored"] += 1
        host = t_entry["host"]
        in_prod = host in prod_files
        tallies["files"]["in prod Plex" if in_prod else "not in prod Plex"] += 1
        variants = {
            "high_no_chapters": redecide(entry, publish_when="high", drop=("chapters",)),
            "medium_no_chapters": redecide(entry, publish_when="medium", drop=("chapters",)),
            "medium": redecide(entry, publish_when="medium"),
            "high_replay": redecide(entry, publish_when="high"),
        }
        for mtype in ("intro", "credits") if not entry["is_movie"] else ("credits",):
            decision = entry["decisions"][mtype]
            ours = decision.get("marker") if decision["status"] == "decided" else None
            ours_m = (ours["start_ms"], ours["end_ms"]) if ours else None
            replay = variants["high_replay"][mtype]
            if (replay[:2] if replay else None) != ours_m:
                tallies["replay"][f"{mtype} replayed decide() differs from the job"] += 1
            if in_prod:
                has_plex = any(r["type"] == mtype for r in prod_rows[host])
                tallies[f"{mtype} presence (all scored files)"][
                    "both" if ours_m and has_plex else "ours only" if ours_m else "Plex only" if has_plex else "neither"
                ] += 1
            medium = variants["medium"][mtype]
            if medium and not ours_m:
                tallies["medium adds"][mtype] += 1
                pm_any, _ = plex_marker(prod_rows[host], mtype, {"start": medium[0]})
                if pm_any:
                    delta = medium[0] - pm_any[0] if mtype == "credits" else medium[1] - pm_any[1]
                    bucket = (
                        "within 3 s of Plex"
                        if abs(delta) <= TOL_MS
                        else "earlier than Plex by >3 s"
                        if delta < 0
                        else "later than Plex by >3 s"
                    )
                    tallies["medium adds"][f"{mtype} {'start' if mtype == 'credits' else 'end'} {bucket}"] += 1
                else:
                    tallies["medium adds"][f"{mtype} Plex has none"] += 1
            t = t_entry["truth"].get(mtype)
            by = "chapters" if ours and "chapters" in ours["decided_by"] else "online agreement" if ours else "none"
            tallies[f"{mtype} ours status"][decision["status"]] += 1
            if not t:
                tallies[f"{mtype} no truth"][f"ours {'marker' if ours else 'none'}"] += 1
                if in_prod:
                    pm = [r for r in prod_rows[host] if r["type"] == mtype]
                    tallies[f"{mtype} no truth"][f"plex {'marker' if pm else 'none'}"] += 1
                continue
            after = mtype == "credits" and content_after_credits(t_entry, t["end"])
            g_ours = grade(mtype, ours_m, t, after)
            tallies[f"{mtype} ours"][g_ours] += 1
            tallies[f"{mtype} ours by basis"][f"{by}: {g_ours}"] += 1
            for vname, dec in variants.items():
                if vname == "high_replay":
                    continue
                m = dec[mtype]
                tallies[f"{mtype} {vname}"][grade(mtype, m[:2] if m else None, t, after)] += 1
            show = show_of(entry["file"])
            shows[show][f"{mtype} ours {g_ours.split(':')[0]}"] += 1
            row = {
                "file": entry["file"],
                "type": mtype,
                "truth": t,
                "after_content": after,
                "ours": ours_m,
                "ours_by": ours["decided_by"] if ours else None,
                "ours_status": decision["status"],
                "ours_reason": decision["reason"],
                "ours_grade": g_ours,
                "variants": {k: v[mtype] for k, v in variants.items()},
            }
            if in_prod:
                pm, count = plex_marker(prod_rows[host], mtype, t)
                g_plex = grade(mtype, pm, t, after)
                tallies[f"{mtype} plex"][g_plex] += 1
                if count > 1:
                    tallies[f"{mtype} plex"]["(files with more than one Plex row of this type)"] += 1
                shows[show][f"{mtype} plex {g_plex.split(':')[0]}"] += 1
                ours_ok, plex_ok = g_ours == "right", g_plex == "right"
                tallies[f"{mtype} head to head"][
                    "both right"
                    if ours_ok and plex_ok
                    else "ours right, Plex not"
                    if ours_ok
                    else "Plex right, ours not"
                    if plex_ok
                    else "neither right"
                ] += 1
                tallies[f"{mtype} presence"][
                    "both have one" if ours_m and pm else "ours only" if ours_m else "Plex only" if pm else "neither"
                ] += 1
                if ours_m and pm:
                    tallies[f"{mtype} both have one"][
                        "both right"
                        if ours_ok and plex_ok
                        else "ours right, Plex wrong"
                        if ours_ok
                        else "Plex right, ours wrong"
                        if plex_ok
                        else "both wrong"
                    ] += 1
                row.update({"plex": pm, "plex_grade": g_plex, "plex_rows": count})
                for tol in (5_000, 10_000):
                    tallies[f"{mtype} right within {tol // 1000} s"]["ours"] += (
                        grade(mtype, ours_m, t, after, tol) == "right"
                    )
                    tallies[f"{mtype} right within {tol // 1000} s"]["plex"] += (
                        grade(mtype, pm, t, after, tol) == "right"
                    )
            detail.append(row)
    result = {
        "tallies": {k: dict(sorted(v.items())) for k, v in sorted(tallies.items())},
        "shows": {k: dict(sorted(v.items())) for k, v in sorted(shows.items())},
        "detail": detail,
    }
    (OUT / f"score-{name}.json").write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps(result["tallies"], indent=1, ensure_ascii=False))
    return result


def main(argv: list[str]) -> int:
    if argv[:1] == ["pick"]:
        pick()
        mounts = Path(__file__).resolve().parent / "scale_mounts.sh"
        mounts.write_text("#!/bin/bash\n# Generated by ./scale_score.py pick (git-ignored: it lists real library folders). Sourced by up.sh.\n" + mv_block() + "\n")
        mounts.chmod(0o600)
        print(f"wrote {mounts.name}; run ./up.sh recreate and ./app.sh recreate to mount it")
        return 0
    if argv[:1] == ["truth"]:
        build_truth()
        return 0
    if argv[:1] == ["score"]:
        score(argv[1] if len(argv) > 1 else "backfill")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
