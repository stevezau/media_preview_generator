"""S10 (HIGH repro): default settings, the owner's real R&M S01 Blu-ray files (read-only stat + ffprobe), the owner's
prod Plex markers (evidence/lab/prod_plex_truth.txt) as what Plex serves, IntroDB/SkipDB answers recorded in
evidence/online. The pipeline publishes a credits marker that swallows the post-credits scene and replaces Plex's own
split credits markers. Subtitle cues in the owner's .en.srt prove dialogue inside the published skip."""
import json, re, glob
from harness import *
from media_preview_generator.markers.sources import introdb as introdb_mod, skipdb as skipdb_mod
exec(open(f"{HERE}/online43_decide.py").read().split("def verdict")[0])
SEASON = "/data_16tb2/TV Shows/Rick and Morty (2013) {tvdb-275274}/Season 01"
plex_rows = {}
for line in open(f"{EVIDENCE}/lab/prod_plex_truth.txt"):
    f, t, s, e, extra, dur, _h = line.rstrip("\n").split("|")
    m = re.search(r"Rick and Morty.*S01E(\d+)", f)
    if m and t:
        plex_rows.setdefault(int(m[1]), []).append({"type": t, "start_ms": int(s), "end_ms": int(e), "final": '"pv:final":"1"' in extra})
answers = {r["case"]["episode"]: r for r in cases if r["case"]["show"] == "Rick and Morty"}
sources = [{"id": s, "enabled": s != "theintrodb", **({"api_key": ""} if s == "theintrodb" else {})}
           for s in ("chapters", "theintrodb", "introdb", "skipdb", "season_audio", "credits_text", "server_markers")]
for ep in (6, 7, 8):
    path = glob.glob(f"{SEASON}/*S01E{ep:02d}*.mkv")[0]
    r = answers[ep]
    idb = FakeClient(LookupResult("ok", tuple(introdb_mod._candidates(r["idb"]))))
    sk = skipdb_mod._candidates(skip_segments(r["case"]))
    skip = FakeClient(LookupResult("ok", tuple(sk)) if sk else NO_DATA)
    registry = FakeRegistry({"plex-1": server_config("plex-1", ServerType.PLEX, root="/data_16tb2/TV Shows")})
    plex = registry.get("plex-1")
    plex.get_external_ids.return_value = {"kind": "episode", "tmdb": "60625", "imdb": "tt2861424", "tvdb": "275274", "season": 1, "episode": ep}
    plex.get_markers.return_value = plex_rows[ep]
    pub = ready_publisher("plex_db")
    store = MarkerStore(tempfile.mkdtemp(prefix="auditB-s10-") + "/markers.db")
    ctx = ctx_for(store, registry, sources=sources, clients={"introdb": idb, "skipdb": skip})
    with patch.object(pipeline, "publisher_for", side_effect=lambda server, cfg, **kw: pub):
        out = check_item(ProcessableItem(canonical_path=path, server_id="", item_id_by_server={}, title="t"), ctx=ctx)
    written = pub.write.call_args.args[1] if pub.write.call_args else []
    print(f"S01E{ep:02d}: Plex serves credits {[(x['start_ms']/1000, x['end_ms']/1000, 'final' if x['final'] else 'non-final') for x in plex_rows[ep] if x['type']=='credits']}")
    print(f"   IntroDB outro {[(c.start_ms/1000, c.end_ms/1000) for c in idb.result.candidates if c.type is T.CREDITS]}, SkipDB {[(c.start_ms/1000, c.end_ms/1000) for c in sk if c.type is T.CREDITS]}")
    print(f"   written to Plex: {[(m.type.value, m.start_ms/1000, m.end_ms/1000, m.decided_by) for m in written]} | outcome {out.outcome_key}")
