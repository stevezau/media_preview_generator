"""S13: Plex serves ONE marker set per item. For a two-version item (web cut + Blu-ray cut with a longer cold open),
Plex's own markers describe one version but are read as evidence for both. A duration-agnostic crowd answer for the
web cut + Plex's web-cut marker = "two sources agree" for the Blu-ray file, at the default level; Jellyfin (one item
per version) then gets a wrong intro on the Blu-ray version."""
from harness import *
tmp = tempfile.mkdtemp(prefix="auditB-s13-")
root = tmp + "/media"
web = make_file(tmp, "media/TV/Demon Slayer (2019) {tvdb-348545}/Season 03/Demon Slayer (2019) - S03E05 [WEBDL-1080p].mkv")
bluray = make_file(tmp, "media/TV/Demon Slayer (2019) {tvdb-348545}/Season 03/Demon Slayer (2019) - S03E05 [Bluray-1080p].mkv")
bd_probe = MediaProbe(1_444_574, (Chapter(0, 105_272, "Chapter 1"), Chapter(105_272, 194_986, "Chapter 2"), Chapter(194_986, 1_444_574, "Chapter 3")))
idb = FakeClient(LookupResult("ok", (Candidate(T.INTRO, 24_046, 114_105, Source.INTRODB),)))
registry = FakeRegistry({"plex-1": server_config("plex-1", ServerType.PLEX, root=root),
                         "jf-1": server_config("jf-1", ServerType.JELLYFIN, root=root)})
for sid in ("plex-1", "jf-1"):
    registry.get(sid).get_external_ids.return_value = {"kind": "episode", "imdb": "tt9335498", "tvdb": "348545", "season": 3, "episode": 5}
plex = registry.get("plex-1")
plex.resolve_remote_path_to_item_id.return_value = "777"          # both versions are Plex item 777
plex.get_markers.return_value = [{"type": "intro", "start_ms": 24_500, "end_ms": 113_900, "final": False}]  # detected on the web version
# Item 777's two parts (asked only by the fixed code): the Blu-ray file and a WEB cut modeled 81 s shorter (the cold-open
# difference between the two OP positions).
plex.get_part_durations.return_value = [1_444_574, 1_363_574]
registry.get("jf-1").resolve_remote_path_to_item_id.side_effect = lambda p, **kw: "jf-bd" if "Bluray" in p else "jf-web"
jf_pub = ready_publisher("jellyfin_bridge", types=("intro", "credits", "recap", "preview"))
pubs = {"jf-1": jf_pub}   # Plex publishing left out: the wrong decision is what matters here
store = MarkerStore(tmp + "/markers.db")
sources = [{"id": "chapters", "enabled": True}, {"id": "introdb", "enabled": True}, {"id": "skipdb", "enabled": True},
           {"id": "server_markers", "enabled": True}]
ctx = ctx_for(store, registry, sources=sources, clients={"introdb": idb, "skipdb": FakeClient(NO_DATA)}, detect={"intro": True, "credits": False})
out = run(ctx, bluray, pubs, bd_probe)
print("Blu-ray version (OP = chapter 2, 105.3-195.0 s):")
show(store, bluray, out)
write = jf_pub.write.call_args
print("  written to Jellyfin:", "nothing" if write is None else (write.args[0], [(m.start_ms, m.end_ms) for m in write.args[1]]))
