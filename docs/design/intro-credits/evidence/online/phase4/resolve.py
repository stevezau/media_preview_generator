"""The owner's TV library -> anime -> a MAL id and the episode number AniSkip expects.

Reads the phase-2 read-only dump of prod Plex's parts; queries no server. Writes ``resolved.json``
(git-ignored: it holds real paths) and prints the counts that ``eval/aniskip-facts.md`` §4 reports.
"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
# The dump is git-ignored, so a worktree doesn't have it; MARKERS_PARTS_DUMP points at the main checkout's copy.
PARTS = Path(os.environ.get("MARKERS_PARTS_DUMP") or HERE / "../../lab/results/scale/prod_plex_parts.json")

TVDB_TOKEN = re.compile(r"\{tvdb-(\d+)\}", re.I)
SXXEYY = re.compile(r"S(\d{1,4})E(\d{1,4})", re.I)
# Sonarr's anime naming puts the absolute number right after the SxxEyy token: "... - S01E02 - 002 - Title ...".
SE_ABS = re.compile(r"S\d{1,4}E\d{1,4}(?:E\d{1,4})?\s+-\s+(\d{1,4})\s+-\s", re.I)
ANIME_ID_TOKEN = re.compile(r"\{(anidb|mal|myanimelist|anilist)-", re.I)
TV_SECTION = 2
EPISODE_TYPE = 4


def load_maps() -> tuple[dict[int, int], set[int], dict[int, list[dict]]]:
    """(anidb -> mal, every tvdb id the lists know, tvdb -> its season/offset rules)."""
    fribb = json.loads((HERE / "fribb-full.json").read_text())
    anidb_to_mal = {int(e["anidb_id"]): int(e["mal_id"]) for e in fribb if e.get("anidb_id") and e.get("mal_id")}
    tvdb_known = {int(e["tvdb_id"]) for e in fribb if isinstance(e.get("tvdb_id"), int)}

    rules: dict[int, list[dict]] = defaultdict(list)
    for anime in ET.parse(HERE / "animelists.xml").getroot().findall("anime"):
        tvdb = anime.get("tvdbid") or ""
        if not tvdb.isdigit():
            continue
        try:
            offset = int(anime.get("episodeoffset") or "0")
        except ValueError:
            offset = 0
        rules[int(tvdb)].append(
            {"season": anime.get("defaulttvdbseason") or "", "offset": offset, "anidb": int(anime.get("anidbid"))}
        )
        tvdb_known.add(int(tvdb))
    return anidb_to_mal, tvdb_known, rules


def episodes() -> list[dict]:
    """Every TV episode part with a tvdb id on its show folder and SxxEyy in its name."""
    parts = json.loads(PARTS.read_text())
    out = []
    tokens = Counter()
    anime_token_paths = 0
    for part in parts:
        if part["mtype"] != EPISODE_TYPE or part["section"] != TV_SECTION:
            continue
        tokens["parts"] += 1
        anime_token_paths += bool(ANIME_ID_TOKEN.search(part["file"]))
        bits = part["file"].split("/")
        if len(bits) < 4:
            continue
        show, name = bits[3], bits[-1]
        tvdb, code = TVDB_TOKEN.search(show), SXXEYY.search(name)
        tokens["tvdb token on the show folder" if tvdb else "no tvdb token"] += 1
        if not tvdb or not code:
            continue
        absolute = SE_ABS.search(name)
        out.append(
            {
                "file": part["file"],
                "show": show,
                "tvdb": int(tvdb.group(1)),
                "season": int(code.group(1)),
                "episode": int(code.group(2)),
                "abs": int(absolute.group(1)) if absolute else None,
                "duration": part.get("duration"),
            }
        )
    print(f"TV episode parts: {tokens['parts']}  {dict(tokens)}")
    print(f"episode paths carrying an anidb/mal/anilist token anywhere: {anime_token_paths}")
    print(f"episodes with a tvdb id and SxxEyy: {len(out)}")
    return out


def resolve(row: dict, anidb_to_mal: dict[int, int], rules: dict[int, list[dict]]) -> tuple[dict | None, str]:
    """(the row plus mal/mal_ep, or None) and the reason it failed.

    The AniDB<->TVDB list gives one rule per MAL entry: the TVDB season it starts in and how many of that season's
    episodes come before it. ``defaulttvdbseason="a"`` means the show is numbered absolutely, so the file's own
    absolute number is the only usable input.
    """
    candidates = rules.get(row["tvdb"], [])
    if not candidates:
        return None, "tvdb id not in the AniDB<->TVDB list"
    seasonal = [r for r in candidates if r["season"] == str(row["season"])]
    number, how = row["episode"], "seasonal"
    if not seasonal:
        seasonal = [r for r in candidates if r["season"] == "a"]
        if not seasonal:
            return None, "no rule for that TVDB season"
        if row["abs"] is None:
            return None, "absolute-numbered show, no absolute number in the file name"
        number, how = row["abs"], "absolute"
    # Several MAL entries can share a TVDB season; the right one is the last whose offset is below this episode.
    best = None
    for rule in sorted(seasonal, key=lambda r: r["offset"]):
        if number > rule["offset"]:
            best = rule
    if best is None:
        return None, "episode number below every offset"
    mal = anidb_to_mal.get(best["anidb"])
    if mal is None:
        return None, "the AniDB id has no MAL id"
    return {**row, "mal": mal, "mal_ep": number - best["offset"], "offset": best["offset"], "how": how}, ""


def main() -> None:
    """Resolve the library and write ``resolved.json``."""
    anidb_to_mal, tvdb_known, rules = load_maps()
    rows = episodes()
    anime = [r for r in rows if r["tvdb"] in tvdb_known or r["tvdb"] in rules]
    # Written out so the Sonarr cross-check sees every anime show, not only the ones that resolved.
    folders: dict[str, str] = {}
    for row in anime:
        folders.setdefault(str(row["tvdb"]), row["show"])
    (HERE / "anime_shows.json").write_text(json.dumps(folders))
    print(
        f"anime show folders: {len({r['show'] for r in anime})}  distinct tvdb ids: {len(folders)}  "
        f"anime episode files: {len(anime)}"
    )

    resolved, why = [], Counter()
    for row in anime:
        got, reason = resolve(row, anidb_to_mal, rules)
        if got is None:
            why[reason] += 1
        else:
            resolved.append(got)
    print(f"resolved to a MAL id + episode number: {len(resolved)} ({len(resolved) / len(anime):.1%})")
    print("  by path:", dict(Counter(r["how"] for r in resolved)))
    print("  unresolved:", dict(why))
    (HERE / "resolved.json").write_text(json.dumps(resolved))


if __name__ == "__main__":
    main()
