"""S1: a normal run stops as soon as chapters decide, so rule 3's veto (two agreeing independent sources contradict the
chapter) never runs; a forced re-detect of the same file puts the chapter in review and unpublishes it.

File: an episode whose generic "Intro" chapter covers the cold open (0-95 s) and whose theme sits in an unnamed chapter
(95-126 s) -- the Mushoku Tensei layout minus the "OP" name. IntroDB and SkipDB (duration-matched) agree on 95-126 s.
"""
from harness import *
tmp = tempfile.mkdtemp(prefix="auditB-s1-")
root = tmp + "/media"
path = make_file(tmp, "media/TV/Some Show (2020) {tvdb-999}/Season 01/Some Show (2020) - S01E03.mkv")
DUR = 1_420_000
probe = MediaProbe(DUR, (Chapter(0, 95_000, "Intro"), Chapter(95_000, 126_000, "Chapter 2"),
                         Chapter(126_000, 1_300_000, "Chapter 3"), Chapter(1_300_000, None, "Chapter 4")))
idb = FakeClient(LookupResult("ok", (Candidate(T.INTRO, 95_500, 126_000, Source.INTRODB),)))
skip = FakeClient(LookupResult("ok", (Candidate(T.INTRO, 96_000, 125_400, Source.SKIPDB),)))
registry = FakeRegistry({"jf-1": server_config("jf-1", ServerType.JELLYFIN, root=root)})
registry.get("jf-1").get_external_ids.return_value = {"kind": "episode", "imdb": "tt7654321", "tvdb": "999", "season": 1, "episode": 3}
pubs = {"jf-1": ready_publisher("jellyfin_bridge", types=("intro", "credits", "recap", "preview"))}
sources = [{"id": "chapters", "enabled": True}, {"id": "introdb", "enabled": True}, {"id": "skipdb", "enabled": True}]
store = MarkerStore(tmp + "/markers.db")
for force in (False, True, False):
    ctx = ctx_for(store, registry, sources=sources, clients={"introdb": idb, "skipdb": skip}, force=force,
                  detect={"intro": True, "credits": False})
    out = run(ctx, path, pubs, probe)
    print(f"run force={force}: introdb asked {len(idb.calls)}x, skipdb asked {len(skip.calls)}x (cumulative)")
    show(store, path, out)
