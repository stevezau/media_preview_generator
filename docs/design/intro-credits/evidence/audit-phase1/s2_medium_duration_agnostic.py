"""S2: at publish_when=medium, a duration-agnostic crowd answer for another cut publishes alone.

Real file (read-only stat + ffprobe, nothing written under /data): the owner's Blu-ray Demon Slayer S03E05. Its OP is
chapter 2 (105.3-195.0 s). IntroDB answered (live, 2026-09-14) intro 24.046-114.105 s for tt9335498 S03E05; TheIntroDB
answered 24.881-123.242 s and returned the identical body for duration_ms=1444574 and duration_ms=2400000.
"""
import json
from harness import *
from media_preview_generator.markers.sources import introdb as introdb_mod, theintrodb as tidb_mod
real = "/data_16tb3/TV Shows/Demon Slayer Kimetsu no Yaiba (2019) {tvdb-348545}/Season 03/Demon Slayer - Kimetsu no Yaiba (2019) - S03E05 - Things Are Gonna Get Real Flashy!! [Bluray-1080p][FLAC 2.0][JA+EN][x264]-AppL3.mkv"
idb_body = json.loads('{"imdb_id":"tt9335498","season":3,"episode":5,"intro":{"start_sec":24.046,"end_sec":114.105,"start_ms":24046,"end_ms":114105,"confidence":1,"submission_count":2},"recap":null,"outro":{"start_sec":1290.167,"end_sec":1380.1,"start_ms":1290167,"end_ms":1380100,"confidence":1,"submission_count":1}}')
tidb_body = json.loads('{"tmdb_id":85937,"type":"tv","season":3,"episode":5,"intro":[{"start_ms":24881,"end_ms":123242}],"credits":[{"start_ms":1290167,"end_ms":1380100}],"preview":[{"start_ms":1380100,"end_ms":null}]}')
idb = FakeClient(LookupResult("ok", tuple(introdb_mod._candidates(idb_body))))
tidb = FakeClient(LookupResult("ok", tuple(tidb_mod._candidates(tidb_body))))
tmp = tempfile.mkdtemp(prefix="auditB-s2-")
root = "/data_16tb3/TV Shows"
registry = FakeRegistry({"jf-1": server_config("jf-1", ServerType.JELLYFIN, root=root)})
registry.get("jf-1").get_external_ids.return_value = {"kind": "episode", "tmdb": "85937", "imdb": "tt9335498", "tvdb": "348545", "season": 3, "episode": 5}
pubs = {"jf-1": ready_publisher("jellyfin_bridge", types=("intro", "credits", "recap", "preview"))}
from media_preview_generator.markers.probe import probe_media
print("file chapters:", [(c.start_ms, c.title) for c in probe_media(real, ffprobe="ffprobe").chapters])
for label, sources, clients in (
    ("medium, default sources (IntroDB on, TheIntroDB off)",
     [{"id": "chapters", "enabled": True}, {"id": "introdb", "enabled": True}, {"id": "skipdb", "enabled": True}], {"introdb": idb}),
    ("medium, TheIntroDB only",
     [{"id": "chapters", "enabled": True}, {"id": "theintrodb", "enabled": True}, {"id": "introdb", "enabled": False}], {"theintrodb": tidb}),
):
    store = MarkerStore(tempfile.mkdtemp(prefix="auditB-s2-db-") + "/markers.db")
    ctx = ctx_for(store, registry, sources=sources, publish_when="medium", clients=clients)
    with patch.object(pipeline, "publisher_for", side_effect=lambda server, cfg, **kw: pubs.get(cfg.id)):
        out = check_item(ProcessableItem(canonical_path=real, server_id="", item_id_by_server={}, title="t"), ctx=ctx)
    print(label)
    show(store, real, out)
