"""S3: server markers count as an independent group, but a Jellyfin/Emby server's segments can be an import of the
same crowd DB (TheIntroDB/IntroDB plugins). One IntroDB answer + its copy on Jellyfin = "two sources agree" at the
DEFAULT publish level ("high"), with IntroDB's times (Demon Slayer S03E05 live answer; the file's OP is 105-195 s).
The published set then goes to every owner, including a Plex server that had no marker."""
from harness import *
tmp = tempfile.mkdtemp(prefix="auditB-s3-")
root = tmp + "/media"
path = make_file(tmp, "media/TV/Demon Slayer (2019) {tvdb-348545}/Season 03/Demon Slayer (2019) - S03E05.mkv")
DUR = 1_444_574
probe = MediaProbe(DUR, (Chapter(0, 105_272, "Chapter 1"), Chapter(105_272, 194_986, "Chapter 2"),
                         Chapter(194_986, 1_291_373, "Chapter 3"), Chapter(1_291_373, None, "Chapter 4")))
idb = FakeClient(LookupResult("ok", (Candidate(T.INTRO, 24_046, 114_105, Source.INTRODB),)))
registry = FakeRegistry({
    "plex-1": server_config("plex-1", ServerType.PLEX, root=root),
    "jf-1": server_config("jf-1", ServerType.JELLYFIN, root=root, markers={"enabled": False, "library_ids": None}),
})
for sid in ("plex-1", "jf-1"):
    registry.get(sid).get_external_ids.return_value = {"kind": "episode", "imdb": "tt9335498", "tvdb": "348545", "season": 3, "episode": 5}
jf = registry.get("jf-1")
# Segments an IntroDB/TheIntroDB importer plugin put on Jellyfin (not ours: the Bridge store is empty).
jf.get_media_segments.return_value = [{"Type": "Intro", "StartTicks": 24_046 * 10_000, "EndTicks": 114_105 * 10_000}]
jf.get_bridge_markers.return_value = []
jf.get_plugin_names.return_value = ["TheIntroDB"]  # the importer plugin that wrote them (asked only by the fixed code)
pubs = {"plex-1": ready_publisher("plex_db")}
sources = [{"id": "chapters", "enabled": True}, {"id": "introdb", "enabled": True}, {"id": "skipdb", "enabled": True},
           {"id": "server_markers", "enabled": True}]
store = MarkerStore(tmp + "/markers.db")
ctx = ctx_for(store, registry, sources=sources, clients={"introdb": idb, "skipdb": FakeClient(NO_DATA)},
              detect={"intro": True, "credits": False})
out = run(ctx, path, pubs, probe)
show(store, path, out)
