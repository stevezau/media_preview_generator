"""S9: Rick and Morty S01 (owner's prod Plex markers + recorded TheIntroDB/IntroDB answers + SkipDB dump) through the
REAL parsers, server-marker reader shape and decide(). R&M has post-credits scenes: Plex's own markers split the tail
into a non-final credits segment, the stinger, and a final credits segment."""
import json, re
from harness import *
from media_preview_generator.markers.decide import DecisionContext, decide
from media_preview_generator.markers.sources.server_markers import read_server_markers
exec(open(f"{HERE}/online43_decide.py").read().split("def verdict")[0])
plex = {}
for line in open(f"{EVIDENCE}/lab/prod_plex_truth.txt"):
    f, t, s, e, extra, dur, _h = line.rstrip("\n").split("|")
    m = re.search(r"Rick and Morty.*S01E(\d+)", f)
    if m and t:
        plex.setdefault(int(m[1]), []).append({"type": t, "start_ms": int(s), "end_ms": int(e), "final": '"pv:final":"1"' in extra})
class FakePlex:
    def __init__(self, rows): self.rows = rows
    def get_markers(self, item_id): return self.rows
for label, order in (("default sources", ("chapters", "introdb", "skipdb", "server_markers")),
                     ("TheIntroDB enabled", ("chapters", "theintrodb", "introdb", "skipdb", "server_markers"))):
    print("==", label, "(high)")
    for r in cases:
        c = r["case"]
        if c["show"] != "Rick and Morty":
            continue
        rows = plex.get(c["episode"], [])
        credit_rows = sorted((x for x in rows if x["type"] == "credits"), key=lambda x: x["start_ms"])
        cands = []
        if "theintrodb" in order and isinstance(r.get("tidb"), dict):
            cands += theintrodb._candidates(r["tidb"])
        if isinstance(r.get("idb"), dict):
            cands += introdb._candidates(r["idb"])
        cands += skipdb._candidates(skip_segments(c))
        cands += read_server_markers(FakePlex(rows), server_config("plex-1", ServerType.PLEX), "1")
        dur = int(c["dur"] * 1000)
        out = decide(cands, DecisionContext(dur, False, "high", frozenset({T.INTRO, T.CREDITS}), order), {})
        d = out[T.CREDITS]
        if d.status.value != "decided":
            continue
        m = d.marker
        stinger = [(a["end_ms"], b["start_ms"]) for a, b in zip(credit_rows, credit_rows[1:]) if b["start_ms"] - a["end_ms"] > 5000]
        skipped = [(s, e) for s, e in stinger if m.start_ms < e and m.end_ms > s]
        print(f"  S01E{c['episode']:02d} credits {m.start_ms/1000:.1f}-{m.end_ms/1000:.1f} by {m.decided_by} | Plex credits "
              f"{[(x['start_ms']/1000, x['end_ms']/1000, 'final' if x['final'] else '') for x in credit_rows]}"
              + (f" | SKIPS STINGER {[(s/1000, e/1000) for s, e in skipped]}" if skipped else ""))
